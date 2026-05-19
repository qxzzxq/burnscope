/*
 * Minimal "always answer with my IP" DNS server. Standard captive-portal
 * trick — pin every name lookup at the SoftAP gateway so phone OSes find
 * the portal at http://192.168.4.1/.
 *
 * Wire format reference: RFC 1035 §4.1.1. Replies copy the question
 * section verbatim and append a single A-record answer pointing at
 * 192.168.4.1 with a small TTL.
 */

#include "captive_dns.h"

#include <string.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <lwip/sockets.h>

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "captdns";

#define DNS_PORT         53
#define DNS_MAX_LEN      512    /* RFC 1035 UDP cap */
#define AP_IP_LITERAL    "192.168.4.1"

/* RFC 1035 minimal header: id(2)+flags(2)+qd(2)+an(2)+ns(2)+ar(2) = 12 */
#define DNS_HDR_LEN 12

/* Walk past a QNAME (sequence of length-prefixed labels ending with 0). */
static int skip_qname(const uint8_t *buf, int len, int off)
{
    while (off < len) {
        uint8_t l = buf[off];
        if (l == 0) {
            return off + 1;
        }
        if ((l & 0xC0) != 0) {
            /* Pointer compression in a query — extremely rare; treat as
             * malformed and bail. */
            return -1;
        }
        off += 1 + l;
    }
    return -1;
}

static void dns_task(void *arg)
{
    (void)arg;

    int sock = lwip_socket(AF_INET, SOCK_DGRAM, IPPROTO_IP);
    if (sock < 0) {
        ESP_LOGE(TAG, "socket() failed: errno=%d", errno);
        vTaskDelete(NULL);
        return;
    }

    struct sockaddr_in bind_addr = {
        .sin_family = AF_INET,
        .sin_addr.s_addr = htonl(INADDR_ANY),
        .sin_port = htons(DNS_PORT),
    };
    if (lwip_bind(sock, (struct sockaddr *)&bind_addr, sizeof(bind_addr)) < 0) {
        ESP_LOGE(TAG, "bind() failed: errno=%d", errno);
        lwip_close(sock);
        vTaskDelete(NULL);
        return;
    }
    ESP_LOGI(TAG, "captive DNS listening on :%d, redirecting to %s",
             DNS_PORT, AP_IP_LITERAL);

    uint32_t ap_ip = 0;
    inet_pton(AF_INET, AP_IP_LITERAL, &ap_ip);

    uint8_t buf[DNS_MAX_LEN];
    for (;;) {
        struct sockaddr_in src;
        socklen_t src_len = sizeof(src);
        int n = lwip_recvfrom(sock, buf, sizeof(buf), 0,
                              (struct sockaddr *)&src, &src_len);
        if (n < DNS_HDR_LEN) {
            continue;
        }

        /* Flip QR=1 (response), RA=1 (recursion available), keep ID. */
        buf[2] = 0x81;
        buf[3] = 0x80;

        uint16_t qd = ((uint16_t)buf[4] << 8) | buf[5];
        if (qd != 1) {
            /* Only single-question queries get an answer; reply with the
             * original questions and zero answers (treats it as NXish). */
            buf[6] = buf[7] = 0;   /* ANCOUNT = 0 */
            buf[8] = buf[9] = 0;   /* NSCOUNT */
            buf[10] = buf[11] = 0; /* ARCOUNT */
            lwip_sendto(sock, buf, n, 0, (struct sockaddr *)&src, src_len);
            continue;
        }

        int qname_end = skip_qname(buf, n, DNS_HDR_LEN);
        if (qname_end < 0 || qname_end + 4 > n) {
            continue;  /* malformed */
        }
        int qtype_off = qname_end;
        int answer_off = qtype_off + 4;  /* QNAME + QTYPE + QCLASS */
        if (answer_off + 16 > (int)sizeof(buf)) {
            continue;
        }

        /* ANCOUNT = 1 */
        buf[6] = 0; buf[7] = 1;
        buf[8] = buf[9] = 0;
        buf[10] = buf[11] = 0;

        /* Answer: NAME pointer to offset 12, TYPE=A, CLASS=IN, TTL=60,
         * RDLENGTH=4, RDATA=AP IP. */
        uint8_t *a = buf + answer_off;
        a[0] = 0xC0; a[1] = 0x0C;        /* NAME = ptr to QNAME at offset 12 */
        a[2] = 0x00; a[3] = 0x01;        /* TYPE = A */
        a[4] = 0x00; a[5] = 0x01;        /* CLASS = IN */
        a[6] = 0x00; a[7] = 0x00;        /* TTL = 60 */
        a[8] = 0x00; a[9] = 0x3C;
        a[10] = 0x00; a[11] = 0x04;      /* RDLENGTH = 4 */
        memcpy(a + 12, &ap_ip, 4);       /* RDATA = AP IP (network order) */

        int reply_len = answer_off + 16;
        lwip_sendto(sock, buf, reply_len, 0,
                    (struct sockaddr *)&src, src_len);
    }
}

void captive_dns_start(void)
{
    static bool started = false;
    if (started) return;
    started = true;
    xTaskCreate(dns_task, "captive_dns", 4096, NULL, 5, NULL);
}
