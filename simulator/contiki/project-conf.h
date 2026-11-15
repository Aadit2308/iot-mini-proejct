#ifndef PROJECT_CONF_H_
#define PROJECT_CONF_H_

/*
 * Project configuration for the soil-moisture sensor node.
 *
 * FAULT_SCENARIO:
 *   0 normal, 1 invalid (120 %), 2 negative (-10 %), 3 missing,
 *   4 delayed, 5 intermittent, 6 malformed, 7 mixed (default)
 *
 * Override from the command line, e.g.:
 *   make TARGET=cooja FAULT_SCENARIO=3
 */
#ifndef FAULT_SCENARIO
#define FAULT_SCENARIO 7
#endif

/* Report period: 5 seconds (CLOCK_SECOND ticks per second). */
#define SENSOR_INTERVAL (5 * CLOCK_SECOND)

/* Keep the default Contiki log output quiet so the serial line carries
 * mainly sensor reports. */
#define LOG_CONF_LEVEL_MAIN LOG_LEVEL_WARN

#endif /* PROJECT_CONF_H_ */
