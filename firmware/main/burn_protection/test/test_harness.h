#ifndef BURN_IDLE_TEST_HARNESS_H
#define BURN_IDLE_TEST_HARNESS_H

#include <stdio.h>
#include <stdint.h>
#include <stdbool.h>

/*
 * test_harness.h — minimal C99 unit-test harness for the burn_idle host
 * tests. Zero external dependencies; intentionally not a real framework.
 *
 * Define the three globals in exactly one translation unit:
 *
 *     int  g_test_failures = 0;
 *     int  g_test_count    = 0;
 *     const char *g_current_test = "";
 *
 * Write tests as:
 *
 *     TEST(test_name) { TEST_ASSERT(...); TEST_ASSERT_EQ_INT(a, b); }
 *
 * Run them inside main():
 *
 *     RUN_TEST(test_name);
 *     ...
 *     TEST_SUMMARY();
 */

extern int  g_test_failures;
extern int  g_test_count;
extern const char *g_current_test;

#define TEST(name) static void name(void)

#define RUN_TEST(name) do {                                                 \
    g_current_test = #name;                                                 \
    int _before = g_test_failures;                                          \
    g_test_count++;                                                         \
    name();                                                                 \
    printf("%s %s\n",                                                       \
           (g_test_failures == _before) ? "[PASS]" : "[FAIL]", #name);      \
} while (0)

#define TEST_ASSERT(expr) do {                                              \
    if (!(expr)) {                                                          \
        fprintf(stderr, "%s:%d: FAIL %s: %s\n",                             \
                __FILE__, __LINE__, g_current_test, #expr);                 \
        g_test_failures++;                                                  \
        return;                                                             \
    }                                                                       \
} while (0)

#define TEST_ASSERT_EQ_INT(a, b) do {                                       \
    long long _a = (long long)(a);                                          \
    long long _b = (long long)(b);                                          \
    if (_a != _b) {                                                         \
        fprintf(stderr,                                                     \
                "%s:%d: FAIL %s: expected %lld got %lld (%s == %s)\n",      \
                __FILE__, __LINE__, g_current_test, _b, _a, #a, #b);        \
        g_test_failures++;                                                  \
        return;                                                             \
    }                                                                       \
} while (0)

#define TEST_ASSERT_EQ_BOOL(a, b) \
    TEST_ASSERT_EQ_INT((a) ? 1 : 0, (b) ? 1 : 0)

#define TEST_SUMMARY() do {                                                 \
    printf("\n%d tests, %d failures\n", g_test_count, g_test_failures);    \
    return g_test_failures == 0 ? 0 : 1;                                    \
} while (0)

#endif  /* BURN_IDLE_TEST_HARNESS_H */
