# Contiki-NG motes for the smart irrigation project
#
#   sensor_node.c      soil-moisture sensor (reports every 5 s)
#   controller_node.c  irrigation controller mote (Cooja only: receives the
#                      sensor's reports over the radio, drives the pump LED)
#
#   make TARGET=native sensor_node                    # sensor as a Linux process (no Cooja)
#   make TARGET=cooja                                 # both motes (Cooja also builds them)
#   make TARGET=native sensor_node FAULT_SCENARIO=3   # pick a scenario (see project-conf.h)
#
# CONTIKI must point at a Contiki-NG checkout (default: ~/contiki-ng).

CONTIKI_PROJECT = sensor_node controller_node
all: $(CONTIKI_PROJECT)

# NullNet: the sensor broadcasts its report as a raw radio frame; no IPv6/RPL needed.
MAKE_NET = MAKE_NET_NULLNET

ifdef FAULT_SCENARIO
CFLAGS += -DFAULT_SCENARIO=$(FAULT_SCENARIO)
endif

# In Cooja the sensor also transmits over the radio (native has no radio).
ifeq ($(TARGET),cooja)
CFLAGS += -DSENSOR_RADIO=1
endif

CONTIKI ?= $(HOME)/contiki-ng
include $(CONTIKI)/Makefile.include
