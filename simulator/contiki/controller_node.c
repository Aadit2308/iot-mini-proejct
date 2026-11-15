/*
 * Irrigation controller mote for Contiki-NG / Cooja.
 *
 * Receives soil-moisture reports from the sensor mote over the radio
 * (NullNet broadcast, payload "MOISTURE node=<id> seq=<n> value=<percent>")
 * and runs fault-aware pump control on the mote itself:
 *
 *   * malformed report            -> sensor INVALID, pump OFF, control suspended
 *   * value outside 0..100 %      -> sensor INVALID, pump OFF, control suspended
 *   * seq <= last seq             -> old / duplicate / out of order: discarded
 *   * seq jumps                   -> missing reports counted
 *   * no valid report for timeout -> sensor STALE, pump OFF, control suspended
 *   * valid report                -> hysteresis: ON below LOW, OFF above HIGH
 *
 * The pump is LED GREEN, a sensor fault is LED RED (visible in Cooja's
 * Network window with the LED skin).
 *
 * Serial output (Cooja Serial Socket -> Python controller / dashboard):
 *   MOISTURE ...        every received report, verbatim (the Python
 *                       supervisor validates it independently)
 *   CTRL key=value ...  the mote's own decisions and state
 *
 * Serial input (sent by the Python controller when the NETCONF config changes):
 *   CONFIG low=<%> high=<%> timeout=<s>
 */

#include "contiki.h"
#include "net/netstack.h"
#include "net/nullnet/nullnet.h"
#include "dev/leds.h"
#include "dev/serial-line.h"
#include "sys/node-id.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "project-conf.h"

#define MAX_REPORT 64

typedef enum { H_WAITING, H_HEALTHY, H_INVALID, H_STALE } health_t;
static const char *health_name[] = { "waiting", "healthy", "invalid", "stale" };

/* Configuration (defaults match config/settings.json). */
static int cfg_low = 30;
static int cfg_high = 45;
static int cfg_timeout = 15;

/* State. */
static health_t health = H_WAITING;
static uint8_t pump_on;
static uint8_t have_seq;
static unsigned long last_seq;
static int moisture = -1;
static unsigned long last_valid_s;
static unsigned long n_valid, n_invalid, n_missing, n_old, n_timeouts, n_pump_on;

/* Radio input is queued here and handled in the process (not in the callback);
 * several reports can arrive in the same tick (a delayed one plus a new one). */
#define RX_QUEUE 4
static char rx_buf[RX_QUEUE][MAX_REPORT];
static uint8_t rx_head, rx_count;

PROCESS(controller_process, "Irrigation controller");
AUTOSTART_PROCESSES(&controller_process);

/* ------------------------------------------------------------------ */
static void
set_pump(uint8_t on, const char *reason)
{
  if(on == pump_on) {
    return;
  }
  pump_on = on;
  if(on) {
    leds_on(LEDS_GREEN);
    n_pump_on++;
  } else {
    leds_off(LEDS_GREEN);
  }
  printf("CTRL event=PUMP pump=%s reason=%s\n", on ? "on" : "off", reason);
}

static void
set_health(health_t h)
{
  health = h;
  if(h == H_INVALID || h == H_STALE) {
    leds_on(LEDS_RED);
  } else {
    leds_off(LEDS_RED);
  }
}

static void
print_state(const char *event)
{
  printf("CTRL event=%s moisture=%d health=%s pump=%s auto=%s low=%d high=%d timeout=%d "
         "valid=%lu invalid=%lu missing=%lu old=%lu timeouts=%lu\n",
         event, moisture, health_name[health], pump_on ? "on" : "off",
         health == H_HEALTHY ? "active" : "suspended", cfg_low, cfg_high, cfg_timeout,
         n_valid, n_invalid, n_missing, n_old, n_timeouts);
}

static void
sensor_fault(health_t h, const char *event)
{
  set_health(h);
  set_pump(0, event);   /* never irrigate on bad or missing data */
  print_state(event);
}

static void
apply_control(void)
{
  if(health != H_HEALTHY) {
    return;
  }
  if(moisture < cfg_low) {
    set_pump(1, "below_low");
  } else if(moisture > cfg_high) {
    set_pump(0, "above_high");
  }
  /* between thresholds: keep the current state (hysteresis) */
}

/* ------------------------------------------------------------------ */
static void
handle_report(char *text)
{
  unsigned node;
  unsigned long seq;
  char value_str[16];
  char *end;
  long value;

  printf("%s\n", text);   /* forward verbatim to the serial line */

  if(sscanf(text, "MOISTURE node=%u seq=%lu value=%15s", &node, &seq, value_str) != 3) {
    n_invalid++;
    sensor_fault(H_INVALID, "MALFORMED");
    return;
  }

  if(have_seq && seq <= last_seq) {
    n_old++;
    printf("CTRL event=OLD_DISCARDED seq=%lu last=%lu\n", seq, last_seq);
    return;   /* never acts on stale information */
  }
  if(have_seq && seq > last_seq + 1) {
    n_missing += seq - last_seq - 1;
    printf("CTRL event=MISSING count=%lu\n", seq - last_seq - 1);
  }
  have_seq = 1;
  last_seq = seq;

  value = strtol(value_str, &end, 10);
  if(end == value_str || *end != '\0') {
    n_invalid++;
    sensor_fault(H_INVALID, "MALFORMED");
    return;
  }
  if(value < 0 || value > 100) {
    n_invalid++;
    sensor_fault(H_INVALID, "INVALID");
    return;
  }

  n_valid++;
  moisture = (int)value;
  last_valid_s = clock_seconds();
  if(health != H_HEALTHY) {
    set_health(H_HEALTHY);
    print_state("RECOVERED");
  }
  apply_control();
  print_state("VALID");
}

static void
handle_serial(const char *line)
{
  int low, high, timeout;
  if(sscanf(line, "CONFIG low=%d high=%d timeout=%d", &low, &high, &timeout) != 3) {
    return;
  }
  if(low < 0 || high > 100 || high <= low || timeout < 1 || timeout > 300) {
    printf("CTRL event=CONFIG_REJECTED low=%d high=%d timeout=%d\n", low, high, timeout);
    return;
  }
  cfg_low = low;
  cfg_high = high;
  cfg_timeout = timeout;
  apply_control();
  print_state("CONFIG");
}

static void
check_timeout(void)
{
  if(health == H_STALE || n_valid == 0) {
    return;
  }
  if(clock_seconds() - last_valid_s > (unsigned long)cfg_timeout) {
    n_timeouts++;
    sensor_fault(H_STALE, "TIMEOUT");
  }
}

static void
input_callback(const void *data, uint16_t len, const linkaddr_t *src, const linkaddr_t *dest)
{
  char *slot;
  if(rx_count == RX_QUEUE) {
    return;   /* queue full: report lost, like a dropped packet */
  }
  if(len >= MAX_REPORT) {
    len = MAX_REPORT - 1;
  }
  slot = rx_buf[(rx_head + rx_count) % RX_QUEUE];
  memcpy(slot, data, len);
  slot[len] = '\0';
  rx_count++;
  process_poll(&controller_process);
}

/* ------------------------------------------------------------------ */
PROCESS_THREAD(controller_process, ev, data)
{
  static struct etimer tick;

  PROCESS_BEGIN();

  nullnet_set_input_callback(input_callback);
  leds_off(LEDS_ALL);
  printf("# irrigation controller node %u started (low=%d high=%d timeout=%d)\n",
         (unsigned)node_id, cfg_low, cfg_high, cfg_timeout);
  print_state("BOOT");

  etimer_set(&tick, CLOCK_SECOND);
  while(1) {
    PROCESS_WAIT_EVENT();
    if(ev == PROCESS_EVENT_POLL) {
      while(rx_count > 0) {
        handle_report(rx_buf[rx_head]);
        rx_head = (rx_head + 1) % RX_QUEUE;
        rx_count--;
      }
    } else if(ev == serial_line_event_message && data != NULL) {
      handle_serial((const char *)data);
    } else if(ev == PROCESS_EVENT_TIMER && etimer_expired(&tick)) {
      check_timeout();
      etimer_reset(&tick);
    }
  }

  PROCESS_END();
}
