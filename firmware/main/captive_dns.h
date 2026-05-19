#pragma once

/*
 * captive_dns.h — minimal UDP DNS server for the captive-portal AP mode.
 *
 * Spawns a single task on UDP:53 that answers *every* inbound A query
 * with the AP gateway IP (192.168.4.1). This is what tells phones to load
 * the portal automatically when they probe `clients3.google.com` etc.
 *
 * Lifetime is tied to the AP. Started by `provisioning_start`; not
 * intended to be stopped — `esp_restart()` after the user submits creds
 * reclaims the socket.
 */

void captive_dns_start(void);
