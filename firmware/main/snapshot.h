#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/*
 * snapshot.h — per-agent rate-limit snapshot store (RAM-only).
 *
 * Mirrors `docs/wire-format.md`. Two static slots (claude, codex). All
 * mutations serialise on an internal mutex; callers may read by copy via
 * `snapshot_store_get` without holding the mutex themselves.
 */

#define SNAPSHOT_MAX_SESSIONS 3   /* FSD allows future overage/credits row. */
#define SNAPSHOT_AGENT_MAX    16
#define SNAPSHOT_TYPE_MAX     16

typedef struct {
    char    type[SNAPSHOT_TYPE_MAX];
    float   used_pct;     /* [0.0, 1.0] */
    int64_t resets_at;    /* unix seconds (UTC) */
} session_snapshot_t;

typedef struct {
    char    agent[SNAPSHOT_AGENT_MAX];     /* "" => slot empty */
    int64_t captured_at;                   /* unix seconds (from wire) */
    int64_t received_at_us;                /* esp_timer_get_time() at receipt */
    session_snapshot_t sessions[SNAPSHOT_MAX_SESSIONS];
    uint8_t session_count;
} agent_snapshot_t;

/**
 * Result of a `snapshot_store_put` call. The HTTP layer translates
 * `STALE` into 409 Conflict and `NO_SLOT` into 500 — both leave the
 * existing slot untouched.
 */
typedef enum {
    SNAPSHOT_PUT_OK = 0,   /* slot updated, listener invoked */
    SNAPSHOT_PUT_STALE,    /* incoming captured_at older than stored */
    SNAPSHOT_PUT_NO_SLOT,  /* unknown agent + no free slot (defensive) */
} snapshot_put_result_t;

typedef void (*snapshot_listener_t)(const agent_snapshot_t *snap, void *user);

/** Initialise the store. Idempotent. */
void snapshot_store_init(void);

/**
 * Insert / overwrite the snapshot for `snap->agent`. Returns:
 *   - `SNAPSHOT_PUT_OK` on update (listener fired);
 *   - `SNAPSHOT_PUT_STALE` when `snap->captured_at` is strictly less than
 *     the stored slot's `captured_at` (multi-laptop guard; equal values
 *     are accepted so re-pushes after `/health` reconciliation are
 *     idempotent);
 *   - `SNAPSHOT_PUT_NO_SLOT` if no slot matches the agent name and none
 *     is free (cannot happen in MVP with two agents and two slots).
 */
snapshot_put_result_t snapshot_store_put(const agent_snapshot_t *snap);

/** Copy the snapshot for `agent` into `*out`. Returns false if absent. */
bool snapshot_store_get(const char *agent, agent_snapshot_t *out);

/** Number of populated slots. */
size_t snapshot_store_count(void);

/**
 * Iterate occupied slots in insertion order, copying each into `*out` and
 * calling `cb`. Cheap — two slots, copied under mutex.
 */
void snapshot_store_foreach(snapshot_listener_t cb, void *user);

/**
 * Seconds elapsed since the most recent `snapshot_store_put` for `agent`,
 * computed against `esp_timer_get_time()`. Returns -1 if the agent is
 * absent.
 */
int64_t snapshot_store_age_s(const char *agent);

/**
 * Register a listener called whenever any agent's slot is written. Pass
 * NULL to clear. Invoked from the HTTP-server task; the listener must not
 * block long.
 */
void snapshot_store_register_listener(snapshot_listener_t cb, void *user);
