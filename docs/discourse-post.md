# Sensorless homing: five things that cost me a day, and a tool that measures them

Category suggestion: **General Discussion** (or **Developers** if the frame-corruption
point gets traction as a Klipper issue)

---

I spent this week getting sensorless homing working properly on a Voron 2.4
(TMC5160, 48V on XY) and hit several things I could not find written down
anywhere. Posting them because four of the five would have saved me hours, and
one of them is a genuine footgun.

I also ended up writing a tool that does the measuring, which is at the end.
The findings matter more than the tool.

This partly follows on from [Sensorless homing - Leviathan v1.2 - TMC5160 -
won't work](https://klipper.discourse.group/t/sensorless-homing-leviathan-v1-2-tmc5160-wont-work/25062),
which is now closed - same board family, same LDO-42STH48-2004MAH motors. That
thread ended on a disagreement about homing speed and current that I think my
measurements settle, at least for this motor and voltage. Point 5 below is the
direct answer to it.

## 1. On CoreXY, a passing axis exonerates the whole drivetrain

If X homes reliably, your belts, pulleys, motors and grub screws are **proven
good** - because on CoreXY both motors and both belts turn for *either* axis.
An X move is not "the X motor"; it is both.

So the standard advice for a Y-only sensorless problem - "check your belt
tension, check your grub screws" - is not just unhelpful, it is **ruled out by
evidence you already have**. What Y does *not* share with X is its own linear
rails and the gantry mass it alone drags.

I chased belts for a while before working this out. If one axis passes and the
other does not, the difference is mechanical only in the parts they do not
share.

## 2. A false trigger corrupts the coordinate frame, and the next move crashes

This one is worth knowing regardless of whether you tune anything.

Klipper sets the axis to `position_max` after **any** successful home,
including a false trigger. So when StallGuard trips early - say 3.5mm into a
150mm approach - Klipper believes the head is at the far rail while it is
physically still near where it started.

The next *absolute* move is then computed from that fiction. In my case:

- Y tripped falsely at 3.5mm, head physically at ~154mm
- Klipper now believed Y = 300 (then 290 after the backoff)
- A move to `Y150` was executed as **140mm in the wrong direction**
- The gantry drove into the *front* rail

The homing move was fine. The **return** move was the crash. It looked exactly
like a direction problem, and it was not.

If you script anything around sensorless homing, do not trust the coordinate
frame after a home you have not verified reached the rail. Step counters do not
lie about displacement even when the frame does.

## 3. `SET_KINEMATIC_POSITION` needs `enable_force_move`

Minor but easy to lose an hour to. In current Klipper it is registered *inside*
the guard:

```python
self._enable_force_move = config.getboolean("enable_force_move", False)
if self._enable_force_move:
    gcode.register_command('SET_KINEMATIC_POSITION', ...)
```

So without `[force_move] enable_force_move: True` you get "Unknown command",
not a helpful message about the missing section.

## 4. More homing current is not better

I assumed a heavier axis wanted more current. It does not scale like that. On
my Y, at 78mm/s:

| Homing current | Result |
|---|---|
| 0.8A | no working threshold at all |
| **1.0A** | **window found** |
| 1.2A | no working threshold at all |

Too low and the motor **skips** instead of stalling - a skid is soft and
gradual, and StallGuard cannot separate it from acceleration load. Too high and
the stall is blunt. There is a band, and it is narrower than I expected.

Corollary: a threshold is only valid at the current it was tuned at. Change the
current, re-tune.

## 5. `homing_speed` widened the window where current and accel could not

My Y had exactly **one** working threshold value - it worked, but one integer
either side failed. That is fragile: it passes cold and fails warm, because
StallGuard drifts with motor temperature. I watched my X drift from 0.00mm to
0.60mm repeatability across a single warm day and recover once cool.

Sweeping accel and current found nothing better. Raising `homing_speed` did:

| | 78mm/s | 100mm/s |
|---|---|---|
| Working window | 1 value | **2 values** |
| Repeatability spread | 0.93mm | **0.60mm** |
| Rail measurement scatter | 2.70mm | **0.40mm** |

StallGuard infers load from back-EMF, which scales with speed - so a faster
approach gives it a stronger signal to discriminate against. If your window is
one value wide, try speed before anything else.

Keep `coolstep_threshold` paired at roughly 0.83x the homing speed. Too far
below and StallGuard is active during the acceleration ramp and trips on
acceleration load rather than on the rail.

## The thing nobody should skip: measure repeatability

A single successful home proves almost nothing, because **a false trigger also
reports success**. The number that matters is whether the origin lands in the
same place every time.

My Y reached the rail 10 times out of 10 and still wandered **3.54mm**. That
does not look like a failure - it prints as layer shifts.

## The tool

https://github.com/JonethanRoux/voron-sensorless-tuning

MIT. It sweeps the threshold across the driver's whole range, measures real
travel from MCU step counters, then verifies repeatability, homes from a range
of distances, and measures true rail-to-rail travel against `position_max`. On
failure it names what to change and by how much.

Driver-agnostic in design - StallGuard2 and StallGuard4 use different field
names and **opposite** sensitivity directions, and it detects which is fitted.

**Caveats, stated plainly:** tested on exactly one machine - Voron 2.4, CoreXY,
TMC5160, 48V, panels off and chamber unheated. The StallGuard4 paths
(2209/2240) are written from datasheets and have **never been run on
hardware**. Nothing is verified hot. It deliberately drives the toolhead into
the rail, so it is not something to run unattended.

Happy to be told I have any of this wrong - particularly point 2, where I would
like to know whether that is considered expected Klipper behaviour or something
worth raising upstream.
