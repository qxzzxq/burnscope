#include "burn_protection/burn_idle.h"

#include <assert.h>
#include <stddef.h>

static bool is_wake_event(burn_idle_event_t ev)
{
    switch (ev) {
        case BURN_IDLE_EV_MOTION:
        case BURN_IDLE_EV_TOUCH:
        case BURN_IDLE_EV_BUTTON:
        case BURN_IDLE_EV_PUSH:
            return true;
        case BURN_IDLE_EV_TIME:
            return false;
    }
    return false;
}

static burn_idle_state_t state_for_idle_duration(const burn_idle_config_t *cfg,
                                                 int64_t idle_us)
{
    /* OFF check first so an equal dim/off threshold lands in OFF
     * (SM-060): callers may legally configure off == dim to mean
     * "no dim phase, jump straight to off." */
    if (idle_us >= cfg->off_after_us) return BURN_IDLE_OFF;
    if (idle_us >= cfg->dim_after_us) return BURN_IDLE_DIMMED;
    return BURN_IDLE_ACTIVE;
}

static burn_idle_output_t derive_output(const burn_idle_t *sm)
{
    burn_idle_output_t out;
    out.state = sm->state;
    out.changed = false;  /* filled in by the caller */
    switch (sm->state) {
        case BURN_IDLE_ACTIVE:
            out.brightness_pct = sm->cfg.active_brightness_pct;
            out.panel_on = true;
            break;
        case BURN_IDLE_DIMMED:
            out.brightness_pct = sm->cfg.dimmed_brightness_pct;
            out.panel_on = true;
            break;
        case BURN_IDLE_OFF:
        default:
            out.brightness_pct = 0;
            out.panel_on = false;
            break;
    }
    return out;
}

static bool output_eq(burn_idle_output_t a, burn_idle_output_t b)
{
    return a.state == b.state
        && a.brightness_pct == b.brightness_pct
        && a.panel_on == b.panel_on;
}

bool burn_idle_config_valid(const burn_idle_config_t *cfg)
{
    if (cfg == NULL) return false;
    if (cfg->dim_after_us <= 0) return false;
    if (cfg->off_after_us < cfg->dim_after_us) return false;
    if (cfg->active_brightness_pct > 100) return false;
    if (cfg->dimmed_brightness_pct > 100) return false;
    if (cfg->dimmed_brightness_pct > cfg->active_brightness_pct) return false;
    if (cfg->motion_threshold_mg < 0) return false;
    return true;
}

void burn_idle_init(burn_idle_t *sm, burn_idle_config_t cfg)
{
    assert(sm != NULL);
    assert(burn_idle_config_valid(&cfg));

    sm->cfg = cfg;
    sm->state = BURN_IDLE_ACTIVE;
    sm->last_activity_us = 0;
    sm->_prev_output = derive_output(sm);
}

burn_idle_output_t burn_idle_step(burn_idle_t *sm,
                                  burn_idle_event_t ev,
                                  int64_t now_us)
{
    assert(sm != NULL);

    if (is_wake_event(ev)) {
        sm->last_activity_us = now_us;
        sm->state = BURN_IDLE_ACTIVE;
    } else {
        /* EV_TIME — pure re-evaluation against the configured thresholds. */
        int64_t idle = now_us - sm->last_activity_us;
        if (idle < 0) idle = 0;  /* clamp on clock skew / test fuzz */
        sm->state = state_for_idle_duration(&sm->cfg, idle);
    }

    burn_idle_output_t out = derive_output(sm);
    out.changed = !output_eq(out, sm->_prev_output);
    sm->_prev_output = out;
    return out;
}
