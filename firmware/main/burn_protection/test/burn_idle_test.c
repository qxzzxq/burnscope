/*
 * burn_idle_test.c — host unit tests for the pure idle state machine.
 *
 * One function per SM-* test in docs/fsd/oled-burn-in-mitigation-fsd.md
 * § 8.1. Run via `ctest --test-dir <build>` or by invoking the binary
 * directly.
 *
 * SM-070 (no ESP-IDF includes) is enforced by check_purity.sh, not here.
 * SM-071 (host compile) is implicit — if this file builds, SM-071 passes.
 */

#include "test_harness.h"
#include "burn_protection/burn_idle.h"

/* Harness globals — defined here, extern'd in test_harness.h. */
int g_test_failures = 0;
int g_test_count    = 0;
const char *g_current_test = "";

/* ------------------------------------------------------------------ helpers */

#define SEC(n)  ((int64_t)(n) * 1000000LL)
#define MIN(n)  (SEC(n) * 60LL)

static burn_idle_config_t make_default_cfg(void)
{
    burn_idle_config_t cfg = {
        .dim_after_us            = MIN(5),
        .off_after_us            = MIN(30),
        .active_brightness_pct   = 70,
        .dimmed_brightness_pct   = 20,
        .motion_threshold_mg     = 50,
    };
    return cfg;
}

/* Drive the SM forward by issuing EV_TIME at successive moments. Useful
 * helper for tests that need the SM in DIMMED or OFF before exercising
 * the actual behaviour under test. */
static burn_idle_output_t drive_to(burn_idle_t *sm, int64_t now_us)
{
    return burn_idle_step(sm, BURN_IDLE_EV_TIME, now_us);
}

/* ----------------------------------------------------------------- SM-001 */

TEST(test_sm_001_initial_state)
{
    burn_idle_t sm;
    burn_idle_init(&sm, make_default_cfg());
    TEST_ASSERT_EQ_INT(sm.state, BURN_IDLE_ACTIVE);
    TEST_ASSERT_EQ_INT(sm.last_activity_us, 0);
}

/* ----------------------------------------------------------------- SM-002 */

TEST(test_sm_002_dim_transition_timing)
{
    burn_idle_t sm;
    burn_idle_init(&sm, make_default_cfg());

    burn_idle_output_t out = drive_to(&sm, MIN(5) - SEC(1));
    TEST_ASSERT_EQ_INT(out.state, BURN_IDLE_ACTIVE);

    out = drive_to(&sm, MIN(5) + SEC(1));
    TEST_ASSERT_EQ_INT(out.state, BURN_IDLE_DIMMED);
}

/* ----------------------------------------------------------------- SM-003 */

TEST(test_sm_003_off_transition_timing)
{
    burn_idle_t sm;
    burn_idle_init(&sm, make_default_cfg());

    drive_to(&sm, MIN(5) + SEC(1));   /* DIMMED */
    burn_idle_output_t out = drive_to(&sm, MIN(30) + SEC(1));
    TEST_ASSERT_EQ_INT(out.state, BURN_IDLE_OFF);
}

/* ----------------------------------------------------------------- SM-010 */

TEST(test_sm_010_motion_wakes_from_dimmed)
{
    burn_idle_t sm;
    burn_idle_init(&sm, make_default_cfg());

    drive_to(&sm, MIN(5) + SEC(1));   /* → DIMMED */
    burn_idle_output_t out = burn_idle_step(&sm, BURN_IDLE_EV_MOTION,
                                            MIN(5) + SEC(30));
    TEST_ASSERT_EQ_INT(out.state, BURN_IDLE_ACTIVE);
    TEST_ASSERT_EQ_INT(out.brightness_pct, 70);
}

/* ----------------------------------------------------------------- SM-011 */

TEST(test_sm_011_motion_wakes_from_off)
{
    burn_idle_t sm;
    burn_idle_init(&sm, make_default_cfg());

    drive_to(&sm, MIN(30) + SEC(1));  /* → OFF */
    burn_idle_output_t out = burn_idle_step(&sm, BURN_IDLE_EV_MOTION,
                                            MIN(30) + SEC(5));
    TEST_ASSERT_EQ_INT(out.state, BURN_IDLE_ACTIVE);
    TEST_ASSERT_EQ_BOOL(out.panel_on, true);
}

/* ----------------------------------------------------------------- SM-012 */

TEST(test_sm_012_touch_wakes_from_dimmed)
{
    burn_idle_t sm;
    burn_idle_init(&sm, make_default_cfg());

    drive_to(&sm, MIN(5) + SEC(1));   /* → DIMMED */
    burn_idle_output_t out = burn_idle_step(&sm, BURN_IDLE_EV_TOUCH,
                                            MIN(5) + SEC(30));
    TEST_ASSERT_EQ_INT(out.state, BURN_IDLE_ACTIVE);
}

/* ----------------------------------------------------------------- SM-013 */

TEST(test_sm_013_button_wakes_from_off)
{
    burn_idle_t sm;
    burn_idle_init(&sm, make_default_cfg());

    drive_to(&sm, MIN(30) + SEC(1));  /* → OFF */
    burn_idle_output_t out = burn_idle_step(&sm, BURN_IDLE_EV_BUTTON,
                                            MIN(30) + SEC(5));
    TEST_ASSERT_EQ_INT(out.state, BURN_IDLE_ACTIVE);
}

/* ----------------------------------------------------------------- SM-014 */

TEST(test_sm_014_push_wakes_from_off)
{
    burn_idle_t sm;
    burn_idle_init(&sm, make_default_cfg());

    drive_to(&sm, MIN(30) + SEC(1));  /* → OFF */
    burn_idle_output_t out = burn_idle_step(&sm, BURN_IDLE_EV_PUSH,
                                            MIN(30) + SEC(5));
    TEST_ASSERT_EQ_INT(out.state, BURN_IDLE_ACTIVE);
}

/* ----------------------------------------------------------------- SM-020 */

TEST(test_sm_020_brightness_lookup_from_config)
{
    burn_idle_t sm;
    burn_idle_init(&sm, make_default_cfg());

    burn_idle_output_t out = drive_to(&sm, SEC(1));  /* ACTIVE */
    TEST_ASSERT_EQ_INT(out.brightness_pct, 70);
    TEST_ASSERT_EQ_BOOL(out.panel_on, true);

    out = drive_to(&sm, MIN(5) + SEC(1));            /* DIMMED */
    TEST_ASSERT_EQ_INT(out.brightness_pct, 20);
    TEST_ASSERT_EQ_BOOL(out.panel_on, true);

    out = drive_to(&sm, MIN(30) + SEC(1));           /* OFF */
    TEST_ASSERT_EQ_INT(out.brightness_pct, 0);
    TEST_ASSERT_EQ_BOOL(out.panel_on, false);
}

/* ----------------------------------------------------------------- SM-021 */

TEST(test_sm_021_brightness_changes_when_cfg_differs)
{
    burn_idle_config_t cfg = make_default_cfg();
    cfg.active_brightness_pct = 50;

    burn_idle_t sm;
    burn_idle_init(&sm, cfg);
    burn_idle_output_t out = drive_to(&sm, SEC(1));
    TEST_ASSERT_EQ_INT(out.state, BURN_IDLE_ACTIVE);
    TEST_ASSERT_EQ_INT(out.brightness_pct, 50);
}

/* ----------------------------------------------------------------- SM-030 */

TEST(test_sm_030_changed_false_on_repeated_steady_step)
{
    burn_idle_t sm;
    burn_idle_init(&sm, make_default_cfg());

    burn_idle_output_t a = drive_to(&sm, SEC(1));
    burn_idle_output_t b = drive_to(&sm, SEC(2));

    /* Both steps are inside ACTIVE; no field differs from the init
     * baseline (or from each other). The first step must already
     * report changed=false — the post-init baseline is what makes
     * this work. Without it the first step would spuriously claim
     * a change. */
    TEST_ASSERT_EQ_INT(a.state, BURN_IDLE_ACTIVE);
    TEST_ASSERT_EQ_INT(b.state, BURN_IDLE_ACTIVE);
    TEST_ASSERT_EQ_BOOL(a.changed, false);
    TEST_ASSERT_EQ_BOOL(b.changed, false);
}

/* ----------------------------------------------------------------- SM-031 */

TEST(test_sm_031_changed_true_on_transition)
{
    burn_idle_t sm;
    burn_idle_init(&sm, make_default_cfg());

    burn_idle_output_t before = drive_to(&sm, MIN(5) - SEC(1));
    burn_idle_output_t after  = drive_to(&sm, MIN(5) + SEC(1));

    TEST_ASSERT_EQ_INT(before.state, BURN_IDLE_ACTIVE);
    TEST_ASSERT_EQ_INT(after.state,  BURN_IDLE_DIMMED);
    TEST_ASSERT_EQ_BOOL(before.changed, false);
    TEST_ASSERT_EQ_BOOL(after.changed,  true);
}

/* ----------------------------------------------------------------- SM-040 */

TEST(test_sm_040_threshold_just_below)
{
    burn_idle_t sm;
    burn_idle_init(&sm, make_default_cfg());

    burn_idle_output_t out = drive_to(&sm, MIN(5) - 1);
    TEST_ASSERT_EQ_INT(out.state, BURN_IDLE_ACTIVE);
}

/* ----------------------------------------------------------------- SM-041 */

TEST(test_sm_041_threshold_exactly_at)
{
    burn_idle_t sm;
    burn_idle_init(&sm, make_default_cfg());

    /* FR-2.4 uses `>=`; idle == dim_after must transition. */
    burn_idle_output_t out = drive_to(&sm, MIN(5));
    TEST_ASSERT_EQ_INT(out.state, BURN_IDLE_DIMMED);
}

/* ----------------------------------------------------------------- SM-050 */

TEST(test_sm_050_rapid_event_flapping)
{
    burn_idle_t sm;
    burn_idle_init(&sm, make_default_cfg());

    /* 100 alternations of EV_TIME and EV_MOTION, all well below dim_after.
     * State must stay ACTIVE the whole way; the second-and-later steps
     * must all report changed=false because neither event causes any
     * output field to differ from the previous step's output. */
    for (int i = 0; i < 100; ++i) {
        int64_t t = SEC(1) + i * 100;
        burn_idle_event_t ev = (i & 1) ? BURN_IDLE_EV_MOTION : BURN_IDLE_EV_TIME;
        burn_idle_output_t out = burn_idle_step(&sm, ev, t);
        TEST_ASSERT_EQ_INT(out.state, BURN_IDLE_ACTIVE);
        if (i > 0) {
            TEST_ASSERT_EQ_BOOL(out.changed, false);
        }
    }
}

/* ----------------------------------------------------------------- SM-060 */

TEST(test_sm_060_config_edge_equal_thresholds)
{
    burn_idle_config_t cfg = make_default_cfg();
    cfg.dim_after_us = MIN(5);
    cfg.off_after_us = MIN(5);

    TEST_ASSERT_EQ_BOOL(burn_idle_config_valid(&cfg), true);

    burn_idle_t sm;
    burn_idle_init(&sm, cfg);

    /* Just below the shared threshold — still ACTIVE. */
    burn_idle_output_t out = drive_to(&sm, MIN(5) - 1);
    TEST_ASSERT_EQ_INT(out.state, BURN_IDLE_ACTIVE);

    /* At/after the shared threshold — jumps straight to OFF, skipping DIMMED. */
    out = drive_to(&sm, MIN(5));
    TEST_ASSERT_EQ_INT(out.state, BURN_IDLE_OFF);
    TEST_ASSERT_EQ_BOOL(out.panel_on, false);
}

/* ----------------------------------------------------------------- SM-061 */

TEST(test_sm_061_config_edge_off_before_dim)
{
    burn_idle_config_t cfg = make_default_cfg();
    cfg.dim_after_us = MIN(30);
    cfg.off_after_us = MIN(5);    /* invalid: off < dim */

    TEST_ASSERT_EQ_BOOL(burn_idle_config_valid(&cfg), false);
}

/* Additional invariant checks exercised by SM-061's validator path. */

TEST(test_sm_061b_config_rejects_null)
{
    TEST_ASSERT_EQ_BOOL(burn_idle_config_valid(NULL), false);
}

TEST(test_sm_061c_config_rejects_brightness_over_100)
{
    burn_idle_config_t cfg = make_default_cfg();
    cfg.active_brightness_pct = 101;
    TEST_ASSERT_EQ_BOOL(burn_idle_config_valid(&cfg), false);
}

TEST(test_sm_061d_config_rejects_dimmed_brighter_than_active)
{
    burn_idle_config_t cfg = make_default_cfg();
    cfg.active_brightness_pct = 50;
    cfg.dimmed_brightness_pct = 60;
    TEST_ASSERT_EQ_BOOL(burn_idle_config_valid(&cfg), false);
}

/* ------------------------------------------------------------------- main */

int main(void)
{
    RUN_TEST(test_sm_001_initial_state);
    RUN_TEST(test_sm_002_dim_transition_timing);
    RUN_TEST(test_sm_003_off_transition_timing);
    RUN_TEST(test_sm_010_motion_wakes_from_dimmed);
    RUN_TEST(test_sm_011_motion_wakes_from_off);
    RUN_TEST(test_sm_012_touch_wakes_from_dimmed);
    RUN_TEST(test_sm_013_button_wakes_from_off);
    RUN_TEST(test_sm_014_push_wakes_from_off);
    RUN_TEST(test_sm_020_brightness_lookup_from_config);
    RUN_TEST(test_sm_021_brightness_changes_when_cfg_differs);
    RUN_TEST(test_sm_030_changed_false_on_repeated_steady_step);
    RUN_TEST(test_sm_031_changed_true_on_transition);
    RUN_TEST(test_sm_040_threshold_just_below);
    RUN_TEST(test_sm_041_threshold_exactly_at);
    RUN_TEST(test_sm_050_rapid_event_flapping);
    RUN_TEST(test_sm_060_config_edge_equal_thresholds);
    RUN_TEST(test_sm_061_config_edge_off_before_dim);
    RUN_TEST(test_sm_061b_config_rejects_null);
    RUN_TEST(test_sm_061c_config_rejects_brightness_over_100);
    RUN_TEST(test_sm_061d_config_rejects_dimmed_brighter_than_active);
    TEST_SUMMARY();
}
