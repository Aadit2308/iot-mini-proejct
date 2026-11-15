/*
 * Soil-moisture sensor node for Contiki-NG / Cooja.
 *
 * Every SENSOR_INTERVAL (default 5 s) the node reports one reading on the
 * serial port (printf), in the format parsed by controller/fault_detector.py:
 *
 *     MOISTURE node=<id> seq=<n> value=<percent>
 *
 * In Cooja, the "Serial Socket (SERVER)" plugin exposes this output on a TCP
 * port so the Python controller can read it:
 *     python -m controller.main --source serial --serial-port 60001
 *
 * With the `native` target the same program runs as a Linux process:
 *     ./build/native/sensor_node.native | python -m controller.main --source stdin
 *
 * FAULT_SCENARIO (project-conf.h or `make FAULT_SCENARIO=n`) selects the
 * reading pattern; see the SCENARIO_* constants below.
 */

#include "contiki.h"
#include "sys/etimer.h"
#include "sys/node-id.h"
#include "lib/random.h"

#include <stdio.h>
#include <stdint.h>
#include <string.h>

#include "project-conf.h"

#ifndef SENSOR_RADIO
#define SENSOR_RADIO 0
#endif
#if SENSOR_RADIO
#include "net/netstack.h"
#include "net/nullnet/nullnet.h"
#include "dev/leds.h"
#endif

/* ------------------------------------------------------------------ */
#define SCENARIO_NORMAL       0  /* 20 25 35 40 50 repeating            */
#define SCENARIO_INVALID      1  /* injects 120 %                       */
#define SCENARIO_NEGATIVE     2  /* injects -10 %                       */
#define SCENARIO_MISSING      3  /* 4 reports not transmitted           */
#define SCENARIO_DELAYED      4  /* 3 reports held back 4 intervals     */
#define SCENARIO_INTERMITTENT 5  /* random loss (30 %) and delay (10 %) */
#define SCENARIO_MALFORMED    6  /* corrupted report                    */
#define SCENARIO_MIXED        7  /* all of the above                    */

#ifndef FAULT_SCENARIO
#define FAULT_SCENARIO SCENARIO_MIXED
#endif

#ifndef SENSOR_INTERVAL
#define SENSOR_INTERVAL (5 * CLOCK_SECOND)
#endif

/* ------------------------------------------------------------------ */
typedef enum { STEP_SEND, STEP_DROP, STEP_DELAY, STEP_MALFORMED } step_kind_t;

typedef struct {
  step_kind_t kind;
  int16_t value;           /* reading in percent */
  uint8_t delay_ticks;     /* for STEP_DELAY: intervals to hold back */
} step_t;

#define S(v)    { STEP_SEND, (v), 0 }
#define DROP(v) { STEP_DROP, (v), 0 }
#define DLY(v)  { STEP_DELAY, (v), 4 }
#define BAD     { STEP_MALFORMED, 0, 0 }

/* Only the table selected by FAULT_SCENARIO is used; Contiki builds with -Werror. */
#if defined(__GNUC__)
#define MAYBE_UNUSED __attribute__((unused))
#else
#define MAYBE_UNUSED
#endif

static const step_t MAYBE_UNUSED scenario_normal[]   = { S(20), S(25), S(35), S(40), S(50) };
static const step_t MAYBE_UNUSED scenario_invalid[]  = { S(25), S(35), S(120), S(120), S(40), S(50), S(20) };
static const step_t MAYBE_UNUSED scenario_negative[] = { S(25), S(35), S(-10), S(40), S(50), S(20) };
static const step_t MAYBE_UNUSED scenario_missing[]  = { S(25), S(35), DROP(40), DROP(50), DROP(20), DROP(25),
                                            S(40), S(50) };
static const step_t MAYBE_UNUSED scenario_delayed[]  = { S(25), S(40), DLY(50), DLY(20), DLY(25),
                                            S(40), S(50), S(20) };
static const step_t MAYBE_UNUSED scenario_malformed[] = { S(25), BAD, S(40), S(50) };
static const step_t MAYBE_UNUSED scenario_mixed[] = {
  S(20), S(25), S(35), S(40), S(50),                              /* normal    */
  S(25), S(35), S(120), S(40), S(50), S(20),                      /* invalid   */
  S(25), S(35), S(-10), S(40), S(50), S(20),                      /* negative  */
  S(25), BAD, S(40), S(50),                                       /* malformed */
  S(25), S(35), DROP(40), DROP(50), DROP(20), DROP(25), S(40), S(50), /* missing */
  S(25), S(40), DLY(50), DLY(20), DLY(25), S(40), S(50), S(20),   /* delayed   */
};

#define LEN(a) (sizeof(a) / sizeof((a)[0]))

#if FAULT_SCENARIO == SCENARIO_NORMAL || FAULT_SCENARIO == SCENARIO_INTERMITTENT
#define STEPS scenario_normal
#elif FAULT_SCENARIO == SCENARIO_INVALID
#define STEPS scenario_invalid
#elif FAULT_SCENARIO == SCENARIO_NEGATIVE
#define STEPS scenario_negative
#elif FAULT_SCENARIO == SCENARIO_MISSING
#define STEPS scenario_missing
#elif FAULT_SCENARIO == SCENARIO_DELAYED
#define STEPS scenario_delayed
#elif FAULT_SCENARIO == SCENARIO_MALFORMED
#define STEPS scenario_malformed
#else
#define STEPS scenario_mixed
#endif

/* ------------------------------------------------------------------ */
/* Messages held back to emulate network delay (delivered out of order). */
#define MAX_PENDING 8
typedef struct {
  uint8_t used;
  uint32_t seq;
  int16_t value;
  uint8_t ticks_left;
} pending_t;

static pending_t pending[MAX_PENDING];
static uint32_t seq;

/* Print the report on the serial line and, in Cooja (SENSOR_RADIO=1), also
 * broadcast it over the radio to the controller mote (controller_node.c). */
static void
transmit(const char *text)
{
  printf("%s\n", text);
#if SENSOR_RADIO
  nullnet_buf = (uint8_t *)text;
  nullnet_len = (uint16_t)strlen(text);
  NETSTACK_NETWORK.output(NULL);
  leds_toggle(LEDS_YELLOW);   /* blinks in Cooja's Network window */
#endif
}

static void
report(uint32_t s, int16_t value)
{
  static char buf[64];
  snprintf(buf, sizeof(buf), "MOISTURE node=%u seq=%lu value=%d",
           (unsigned)node_id, (unsigned long)s, (int)value);
  transmit(buf);
}

static void
hold_back(uint32_t s, int16_t value, uint8_t ticks)
{
  uint8_t i;
  for(i = 0; i < MAX_PENDING; i++) {
    if(!pending[i].used) {
      pending[i].used = 1;
      pending[i].seq = s;
      pending[i].value = value;
      pending[i].ticks_left = ticks;
      return;
    }
  }
  /* queue full: the message is simply lost */
}

static void
deliver_due(void)
{
  uint8_t i;
  for(i = 0; i < MAX_PENDING; i++) {
    if(pending[i].used) {
      if(pending[i].ticks_left > 0) {
        pending[i].ticks_left--;
      }
      if(pending[i].ticks_left == 0) {
        report(pending[i].seq, pending[i].value);  /* arrives late */
        pending[i].used = 0;
      }
    }
  }
}

static void
run_step(const step_t *step)
{
  seq++;

#if FAULT_SCENARIO == SCENARIO_INTERMITTENT
  {
    unsigned short r = random_rand() % 100;
    if(r < 30) {
      return;                                   /* lost */
    } else if(r < 40) {
      hold_back(seq, step->value, 1 + (random_rand() % 4));
      return;                                   /* delayed */
    }
  }
#endif

  switch(step->kind) {
  case STEP_SEND:
    report(seq, step->value);
    break;
  case STEP_DROP:
    /* simulated packet loss: sequence number consumed, nothing sent */
    break;
  case STEP_DELAY:
    hold_back(seq, step->value, step->delay_ticks);
    break;
  case STEP_MALFORMED:
    {
      static char bad[64];
      snprintf(bad, sizeof(bad), "MOISTURE node=%u seq=%lu value=ERR#",
               (unsigned)node_id, (unsigned long)seq);
      transmit(bad);
    }
    break;
  }
}

/* ------------------------------------------------------------------ */
PROCESS(soil_sensor_process, "Soil moisture sensor");
AUTOSTART_PROCESSES(&soil_sensor_process);

PROCESS_THREAD(soil_sensor_process, ev, data)
{
  static struct etimer timer;
  static uint16_t index;

  PROCESS_BEGIN();

  random_init();   /* Contiki-NG seeds the PRNG from the node address */
  printf("# soil sensor node %u started, scenario=%d, interval=%lu ticks\n",
         (unsigned)node_id, FAULT_SCENARIO, (unsigned long)SENSOR_INTERVAL);

  etimer_set(&timer, SENSOR_INTERVAL);
  while(1) {
    PROCESS_WAIT_EVENT_UNTIL(etimer_expired(&timer));
    etimer_reset(&timer);

    deliver_due();
    run_step(&STEPS[index]);
    index = (index + 1) % LEN(STEPS);
  }

  PROCESS_END();
}
