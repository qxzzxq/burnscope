/*
 * Two-slot per-agent snapshot store. RAM-only (FR-3.3): the daemon
 * re-pushes within ~30 s so persisting is unnecessary.
 */

#include "snapshot.h"

#include <string.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

static const char *TAG = "snapshot";

#define MAX_AGENTS 2

static agent_snapshot_t s_slots[MAX_AGENTS];
static SemaphoreHandle_t s_mutex = NULL;
static snapshot_listener_t s_listener = NULL;
static void *s_listener_user = NULL;

static void lock(void)
{
    if (s_mutex != NULL) {
        xSemaphoreTake(s_mutex, portMAX_DELAY);
    }
}

static void unlock(void)
{
    if (s_mutex != NULL) {
        xSemaphoreGive(s_mutex);
    }
}

void snapshot_store_init(void)
{
    if (s_mutex != NULL) {
        return;
    }
    s_mutex = xSemaphoreCreateMutex();
    configASSERT(s_mutex != NULL);
    memset(s_slots, 0, sizeof(s_slots));
}

bool snapshot_store_put(const agent_snapshot_t *snap)
{
    if (snap == NULL || snap->agent[0] == '\0') {
        return false;
    }
    lock();

    int existing = -1;
    int empty = -1;
    for (int i = 0; i < MAX_AGENTS; ++i) {
        if (s_slots[i].agent[0] == '\0') {
            if (empty < 0) empty = i;
        } else if (strncmp(s_slots[i].agent, snap->agent, SNAPSHOT_AGENT_MAX) == 0) {
            existing = i;
            break;
        }
    }

    int slot = (existing >= 0) ? existing : empty;
    if (slot < 0) {
        unlock();
        ESP_LOGW(TAG, "no free slot for agent '%s'", snap->agent);
        return false;
    }

    s_slots[slot] = *snap;
    s_slots[slot].received_at_us = esp_timer_get_time();

    /* Copy for the listener invocation outside the lock — listeners may
     * touch other modules (LVGL) that can be expensive. */
    agent_snapshot_t copy = s_slots[slot];
    snapshot_listener_t cb = s_listener;
    void *cb_user = s_listener_user;

    unlock();

    if (cb != NULL) {
        cb(&copy, cb_user);
    }
    return true;
}

bool snapshot_store_get(const char *agent, agent_snapshot_t *out)
{
    if (agent == NULL || out == NULL) {
        return false;
    }
    bool found = false;
    lock();
    for (int i = 0; i < MAX_AGENTS; ++i) {
        if (s_slots[i].agent[0] != '\0' &&
            strncmp(s_slots[i].agent, agent, SNAPSHOT_AGENT_MAX) == 0) {
            *out = s_slots[i];
            found = true;
            break;
        }
    }
    unlock();
    return found;
}

size_t snapshot_store_count(void)
{
    size_t n = 0;
    lock();
    for (int i = 0; i < MAX_AGENTS; ++i) {
        if (s_slots[i].agent[0] != '\0') n++;
    }
    unlock();
    return n;
}

void snapshot_store_foreach(snapshot_listener_t cb, void *user)
{
    if (cb == NULL) return;
    agent_snapshot_t copies[MAX_AGENTS];
    int n = 0;

    lock();
    for (int i = 0; i < MAX_AGENTS; ++i) {
        if (s_slots[i].agent[0] != '\0') {
            copies[n++] = s_slots[i];
        }
    }
    unlock();

    for (int i = 0; i < n; ++i) {
        cb(&copies[i], user);
    }
}

int64_t snapshot_store_age_s(const char *agent)
{
    if (agent == NULL) return -1;
    int64_t age = -1;
    lock();
    for (int i = 0; i < MAX_AGENTS; ++i) {
        if (s_slots[i].agent[0] != '\0' &&
            strncmp(s_slots[i].agent, agent, SNAPSHOT_AGENT_MAX) == 0) {
            int64_t now = esp_timer_get_time();
            age = (now - s_slots[i].received_at_us) / 1000000;
            break;
        }
    }
    unlock();
    return age;
}

void snapshot_store_register_listener(snapshot_listener_t cb, void *user)
{
    lock();
    s_listener = cb;
    s_listener_user = user;
    unlock();
}
