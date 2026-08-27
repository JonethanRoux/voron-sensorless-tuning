# Ready-to-paste Reddit posts

Reddit rewards short and specific. Both link the Discourse writeup rather than
duplicating it - splitting a discussion across three sites helps nobody, and
the Discourse thread is the one that stays searchable.

Post as a **text post**, not a link post: link posts get less engagement and
you cannot explain why anyone should care.

---

## r/VORONDesign

**Title:**

> PSA: on CoreXY, an X axis that homes proves your belts and grub screws are fine

**Body:**

Spent the week properly tuning sensorless homing on my 2.4 (Kraken, TMC5160,
48V) and this was the thing I wish someone had told me on day one.

Both motors and both belts turn for **either** axis on CoreXY. An X move is not
"the X motor", it is both of them through both belts. So if X homes reliably,
your motors, belts, pulleys and grub screws are **proven good** - and the
standard reply to a Y-only sensorless problem ("check your belt tension") is
already ruled out by evidence you have.

What Y does *not* share with X is its own linear rails and the gantry mass it
alone drags. That is where to look.

A few other things that cost me hours:

- **A false trigger corrupts your coordinate frame.** Klipper sets the axis to
  `position_max` after *any* successful home, including one that tripped 3mm
  into a 150mm approach. The next absolute move is computed from that fiction -
  mine drove the gantry into the *opposite* rail. Looked exactly like a
  direction fault, wasn't.
- **More homing current is not better.** My Y found a working threshold at 1.0A
  and none at all at either 0.8A or 1.2A.
- **A single successful home proves nothing** - a false trigger also reports
  success. Mine reached the rail 10/10 times and still wandered 3.54mm, which
  prints as layer shifts rather than an obvious failure.

Full writeup with the numbers, and an MIT tool that does the measuring:

https://klipper.discourse.group/t/sensorless-homing-five-things-that-cost-me-a-day-and-a-tool-that-measures-them/26260

Tested on one machine only, panels off, cold. Happy to be told I have any of it
wrong.

---

## r/klippers

**Title:**

> Sensorless homing: a false trigger leaves Klipper's coordinate frame wrong, and the next move crashes

**Body:**

Hit this while tuning StallGuard on a Voron 2.4 (TMC5160) and it took a while
to understand, so posting in case it saves someone else.

Klipper sets the axis to `position_max` after **any** successful home,
including a false trigger. So when StallGuard trips early - say 3.5mm into a
150mm approach - Klipper believes the head is at the far rail while it is
physically still near where it started.

The next *absolute* move is then computed from that fiction:

- Y tripped falsely at 3.5mm, head physically at ~154mm
- Klipper believed Y = 290 after the backoff
- A move to `Y150` executed as 140mm in the **wrong direction**
- Gantry drove into the front rail

The homing move was fine. The **return** move was the crash.

If you script anything around sensorless homing, don't trust the frame after a
home you haven't verified actually reached the rail. MCU step counters don't
lie about displacement even when the frame does.

Two smaller ones:

- `SET_KINEMATIC_POSITION` is registered *inside* the `enable_force_move`
  guard, so without `[force_move] enable_force_move: True` you get "Unknown
  command" rather than anything pointing at the missing section.
- `homing_speed` widened my working threshold window where current and accel
  couldn't - 78 to 100mm/s took it from 1 usable value to 2, and cut rail
  measurement scatter from 2.70mm to 0.40mm. StallGuard reads load from
  back-EMF, so faster gives it a stronger signal.

Writeup and an MIT tool that measures all of it:

https://klipper.discourse.group/t/sensorless-homing-five-things-that-cost-me-a-day-and-a-tool-that-measures-them/26260

Is the frame behaviour after a false trigger considered expected, or worth
raising upstream? Genuinely unsure.
