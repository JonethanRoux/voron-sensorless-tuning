# Sensorless homing tuning for Klipper

Tuning sensorless homing normally means editing a threshold in `printer.cfg`,
restarting, typing `G28 X`, watching what happens, and guessing. This does that
loop for you and tells you what the numbers mean.

It sweeps the StallGuard threshold across the driver's entire range, measures
how far the axis actually travelled on each attempt, and reports which values
reached the rail. Then it proves the result repeats, homes from a spread of
starting distances, and measures the true rail-to-rail travel against what your
config claims.

Driver-agnostic: it detects which StallGuard generation is fitted and adapts.

## Symptoms this is meant to diagnose

If any of these describe your machine, this is the right tool. They are all the
same underlying problem - a StallGuard threshold that triggers on something
other than the rail - and they are listed in the words people actually search
for, because it took me a while to work out they were one fault and not six.

- **Homing succeeds, then the very next move crashes into the opposite side.**
  The home was false, Klipper set the axis to `position_max` anyway, and the
  return move was computed from a position the head was never at. This looks
  exactly like an inverted direction and is not.
- **`Move out of range`** immediately after a home that appeared to work.
- **`Endstop x still triggered after retract`** - the stall flag is still set
  after the backoff, which usually means the threshold is far too sensitive.
- **Layer shifts**, or a first layer that starts a few millimetres off, on a
  machine whose belts and grub screws are fine.
- **Homes reliably cold and fails once the chamber is warm.** StallGuard drifts
  with motor temperature, so a threshold with a one-value-wide working window
  has no margin left by the time the printer is hot.
- **Exactly one threshold value works** and both neighbours fail, so every
  change to current, speed or accel breaks it again.
- **It grinds into the rail and never triggers** at any threshold you have
  tried.
- **One axis is fine and the other is not** on a CoreXY, which narrows the
  cause far more than the usual advice admits - see
  [On CoreXY, a passing axis exonerates the drivetrain](#on-corexy-a-passing-axis-exonerates-the-drivetrain).
- **It homes, but you have never checked whether it homes to the *same place*
  twice.** A single successful home proves almost nothing. Mine reached the
  rail ten times out of ten and still wandered 3.54mm.

Tested on a Voron 2.4 (CoreXY, TMC5160, 48V). Written to work with StallGuard2
(`driver_SGT`, TMC2130/5160/5161) and StallGuard4 (`driver_SGTHRS`,
TMC2209/2240), which run on **opposite** sensitivity scales - see
[Driver support](#driver-support).

> **Tune X first, always.** Not a preference - it changes what a Y result
> means. X moves only the toolhead, so a failed value grinds far more gently
> than one that drives the whole gantry. More importantly, on CoreXY both
> motors and both belts turn for *either* axis, so an X axis that passes
> proves the entire shared drivetrain is sound. Without that baseline, a Y
> failure could be the threshold, the belts, the pulleys, a grub screw or the
> gantry, and you have no way to tell them apart. With it, only the Y rails
> and the gantry mass are left. Doing Y first throws that away.

> ## Use at your own risk
>
> **This software deliberately drives your toolhead into the frame, on
> purpose, repeatedly.** That is how it finds the limits of the working range.
> Belts can skip, printed parts can crack, and a wrong setting can do worse.
>
> It is provided as-is, with no warranty of any kind. **The author accepts no
> liability for any damage, injury or loss** arising from its use - to your
> printer, your parts, your prints, or anything else. You run it on your own
> machine, at your own risk, and you are responsible for supervising it.
>
> Read [Before you run this](#before-you-run-this) and
> [What has actually been tested](#what-has-actually-been-tested) first. If
> you are not comfortable standing over the machine with a hand on the
> emergency stop while it crashes into a rail, do not use this.

## Why a threshold that "works" often isn't tuned

A single successful home proves very little. A false trigger also reports
success, and Klipper sets the axis to `position_max` either way. What matters
is:

- **Does it repeat?** A threshold can reach the rail and still stop somewhere
  different each time. That prints as a layer shift, not as an obvious failure.
- **How wide is the working window?** If exactly one integer works, it will
  work cold and fail warm. StallGuard drifts with motor temperature, so a
  one-value-wide window has no margin for a machine that heats up.
- **Is the travel real?** If the measured rail-to-rail distance is shorter than
  `position_max - position_min`, your config claims travel the machine does not
  have, and every print is squeezed into a frame that does not exist.

The tools report all three.

## What it is

Two parts:

**`scripts/sgt_diag.py`** does the measuring. It drives the machine over
Moonraker's HTTP API and reads raw MCU step counters, which is the only honest
source of how far an axis moved.

**`config/sensorless_tools.cfg`** provides the macros you actually call.

A gcode macro cannot do this work alone, for three reasons:

1. A macro is rendered to text *before* it runs, so it cannot branch on a value
   it measured earlier in the same macro.
2. A failed `G28` aborts the whole macro, so it cannot record "no trigger" and
   continue to the next value.
3. Real travel is only knowable from MCU step counts.

Hence a shell command. Note that `RUN_SHELL_COMMAND` **blocks** the calling
macro, while the script needs the gcode queue to drive the machine - so
`sgt_run.sh` launches it detached. Calling the Python directly deadlocks.

## Before you run this

**This is a tuning tool, not a setup tool.** It assumes your machine already
moves correctly and that sensorless homing is wired and working badly - not
that it is unbuilt or miswired. Every test drives the toolhead into a rail on
purpose, so anything wrong underneath gets driven into a rail too.

Confirm all of the following yourself first. None of it takes long, and each
one is a thing that will otherwise waste hours or break something:

**Directions are correct.** Verify, do not assume. Declare a position, command
a small move, and watch which way the head physically goes:

```
SET_KINEMATIC_POSITION X=150 Y=150
G90
G1 X160 F600      ; must move TOWARD the rail X homes into
G1 Y160 F600      ; must move TOWARD the rail Y homes into
```

If a positive move goes away from the rail that `homing_positive_dir` points
at, stop and fix the direction first. A sweep on an inverted axis measures
nothing useful and drives the gantry into the opposite end.

**The mechanics are sound.** Belts tensioned, pulley grub screws tight, both
axes moving freely by hand with no binding or notchy spots. StallGuard infers
load; a rough axis reads as load and trips early, and no threshold fixes that.

**The steps are right.** A commanded 100mm move should measure 100mm with a
ruler. If `rotation_distance` or `full_steps_per_rotation` is wrong, every
travel figure this tool reports is wrong by the same factor.

**Endstops and limits are honest.** `position_min` and `position_max` should
roughly match the real travel. `MEASURE_X_RAIL` will tell you if they do not,
but it needs a working home first.

**Sensorless homing already triggers at all**, even if badly - a plain `G28 X`
should stop at the rail rather than grinding indefinitely. If it never
triggers at any threshold, the problem is wiring, `homing_speed`, or
`coolstep_threshold`, not the threshold. Run `SENSORLESS_STATUS`, which checks
the rules that must hold.

**You have run the basics.** Motors move the right amount, the frame is
square, the gantry is levelled. Tuning a threshold on a machine with a
mechanical fault produces a number that encodes the fault.

If you are not sure about any of the above, sort that out first. This will
happily spend an hour measuring a broken machine very precisely.

## Requirements

Check these first. Three of them are hard requirements that fail in ways that
are not obvious from the error message.

**1. `gcode_shell_command`** - not part of stock Klipper. Install via
[KIAUH](https://github.com/dw-0/kiauh) ("Advanced" -> "G-Code Shell Command"),
or copy `gcode_shell_command.py` into `~/klipper/klippy/extras/`. Without it
Klipper refuses the config with `Unknown config object 'gcode_shell_command'`.

**2. `[force_move]` with `enable_force_move: True`** - **shipped for you** in
`sensorless_tools.cfg`, so normally you need do nothing. But **delete that
block if you already declare `[force_move]` elsewhere** - Klipper aborts on a
duplicate section, and many Voron configs already have it for `STEPPER_BUZZ`.
One definition serves the whole printer.

It is required because in current Klipper `SET_KINEMATIC_POSITION` is
registered *inside* that guard:

```python
self._enable_force_move = config.getboolean("enable_force_move", False)
if self._enable_force_move:
    gcode.register_command('SET_KINEMATIC_POSITION', ...)
```

The tools use it to reconcile the coordinate frame after a false trigger.

**3. `[respond]`** - every result is reported with `RESPOND`. Add the bare
section to `printer.cfg` if you do not already have it.

**4. Moonraker on `localhost:7125`** - the script drives the machine over the
HTTP API. If yours listens elsewhere, edit `BASE` at the top of `sgt_diag.py`.

**5. Sensorless homing already wired up.** These tools tune a threshold; they
do not set up sensorless homing for you. You need the diag pin connected and
the axis pointed at the virtual endstop:

```
[stepper_x]
endstop_pin: tmc5160_stepper_x:virtual_endstop
homing_retract_dist: 0        # a retract-and-retry cannot work on StallGuard

[tmc5160 stepper_x]
diag1_pin: ^!PC15             # diag0_pin / diag_pin on other drivers
```

`homing_retract_dist: 0` matters: the second touch of a retract-and-retry
starts with StallGuard still loaded from the first, so it triggers immediately.

**6. Python 3 on the host.** Standard library only, no pip packages.

## Install

```bash
cp scripts/sgt_diag.py scripts/sgt_run.sh ~/
chmod +x ~/sgt_run.sh
mkdir -p ~/printer_data/config/homing
cp config/sensorless*.cfg ~/printer_data/config/homing/
```

Add to `printer.cfg`:

```
[include homing/*.cfg]
```

**Paths assume the user is `voron24`.** If yours differs, edit the absolute
paths in `sgt_run.sh`, in `sgt_diag.py` (`CONFIG_FILE`), and in the four
`[gcode_shell_command]` blocks at the top of `sensorless_tools.cfg`. Then
`FIRMWARE_RESTART`, and run `SENSORLESS_STATUS` to confirm it can see your
driver and that the rules below hold.

`sensorless.cfg` also provides the `[homing_override]` that does the actual
homing. If you already have your own, take `sensorless_tools.cfg` alone and
keep yours - but the tools assume homing drops to `home_current` and backs off
afterwards, so read `_SENSORLESS_HOME` before dropping it.

## Walkthrough - tuning an axis from scratch

**Do X first.** It moves only the toolhead, so a grind is gentler, and on
CoreXY a passing X proves the motors, belts, pulleys and grub screws are all
sound - which rules them out if Y then misbehaves.

### 1. Check the rules

```
SENSORLESS_STATUS
```

It prints both axes and checks the things that must hold - `homing_speed`
above `rotation_distance`, `coolstep_threshold` below `homing_speed`. If it
flags either, fix that first. No threshold can compensate.

### 2. Put the gantry in the middle

Sweeps home *into* a rail, so the head needs room to build speed and a real
distance to travel. Starting against the rail it is homing into gives an
instant trip that means nothing.

```
M84
```

Then **push the toolhead to roughly mid-travel by hand**, both axes. For Y
this is mandatory and the macro will remind you: Y is never moved
automatically, because until the first successful home its position is
unmeasured, and moving an axis you cannot locate is how a gantry ends up in a
rail. X is homed for real first, so it looks after itself.

### 3. Sweep

```
FIND_X_SGT
```

Five warnings and a ten second countdown, then it moves. **Stay by the
emergency stop** - but read step 4 before you use it.

### 4. Expect one grind. Do NOT stop it.

A sweep walks from the most sensitive value to the least, and the run looks
like this:

```
sgt=-64  3.5mm  [EARLY]   tripped instantly, harmless
sgt=-63  3.5mm  [EARLY]
...                        many of these, all identical
sgt=0    150.1mm [GOOD ]  reached the rail - this value WORKS
sgt=1    150.6mm [GOOD ]
sgt=2    170.2mm [GOOD ]
sgt=3    458.5mm [FAIL ]  <-- GRINDS. This is expected.
STOPPING - past this point it only grinds the rail.
```

**That last line is the point of the test.** The sweep has to find the value
that is too insensitive to trigger, because the working window is bounded by
it. When it happens the motor drives into the stop and **it sounds bad** - a
loud grinding or buzzing for a second or two as the belts skip.

**Let it finish.** The script detects the failure, stops the sweep itself, and
never tries a worse value. Hitting the emergency stop there aborts the run and
you lose everything measured up to that point - the sweep has to start over.

Reach for the e-stop if something *else* is wrong: the head moving away from
the rail it should home into, a crash into the opposite end, a collision with
a clip or cable, or grinding that continues for more than a few seconds.

The early trips at the sensitive end are the safe failure mode - the axis
barely moves. Over-sensitive fails harmlessly; under-sensitive grinds. That is
why sweeps always start at the sensitive end.

### 5. Read the verdict

On a pass it chains automatically into repeatability, home-range and
rail-measure, and ends with one line:

```
OVERALL: PASS (3 of 3 tests)
```

The numbers that matter:

- **Window width.** `[0,1,2]` is three values wide - good margin. `[1]` alone
  works today and fails when the machine is warm.
- **Spread.** Under 0.5mm is printable. Over 1mm shows as layer shifts.
- **Rail measure.** Should match `position_max - position_min`.

### 6. If the window is narrow, adjust in this order

Change **one** thing, then re-sweep - each of these invalidates a threshold
tuned against the others. The report names actual values to try.

1. **`homing_speed`** (and `coolstep_threshold` with it, at about 0.83x).
   This is the biggest lever. StallGuard reads load from back-EMF, so faster
   gives a stronger signal to discriminate against. On the test machine Y went
   from a 1-wide window at 78mm/s to 2-wide at 100mm/s, and its rail
   measurement tightened from 2.70mm of scatter to 0.40mm. Config-only, so it
   needs a `FIRMWARE_RESTART`.
2. **Homing accel** - `variable_x_home_accel` / `variable_y_home_accel`.
   Acceleration load can sit close to stall load on a heavy axis, leaving no
   gap for a threshold to live in. Runtime, no restart.
3. **`home_current`** via `SET_HOME_CURRENT`, in 0.1A steps. Too low and the
   motor skips instead of stalling, and a skid is soft and gradual where a
   stall is sharp. Too high and the stall is blunt. Note that more is not
   better: on the test machine Y found a window at 1.0A and none at all at
   either 0.8A or 1.2A.
4. **Mechanical** - last, and only if the *other* axis also fails. See the
   CoreXY note below.

`TUNE_X_MATRIX` automates 2 and 3 together across the whole threshold range,
and ranks the combinations by window width and then by measured repeatability.

### 7. Apply and repeat for Y

A passing sweep writes the threshold into `printer.cfg` and tells you a
restart is needed. Then centre the gantry by hand again and run `FIND_Y_SGT`.

Expect Y to be harder. It drags the entire gantry while X moves only the
toolhead, so it loads the motor far more and generally wants its own speed and
current.

## Use

```
FIND_X_SGT          sweep the whole threshold range, step 1
VERIFY_X_HOME       home 10 times, report the spread
MEASURE_X_RAIL      true rail-to-rail travel vs position_max
TEST_X_HOME_RANGE   home from 5, 15, 40, 120 and 250mm off the rail
TUNE_X_MATRIX       sweep accel x threshold, rank by window width
SENSORLESS_STATUS   settings for both axes, and the rules that must hold
SHOW_SGT_LOG        follow a run in progress
SHOW_SGT_RESULT     re-print the last summary
STOP_SGT_DIAG       abort and release the motors
```

`_Y_` variants for the other axis. A passing test chains into the next one, so
`FIND_X_SGT` alone will sweep, verify, range-test, measure, and finish with a
single verdict:

```
sgt=0  150.1mm  GOOD
sgt=1  150.6mm  GOOD
sgt=2  170.2mm  GOOD
sgt=3  458.5mm  FAIL

-- repeatability --
sgt=1  10 runs  151.2mm  spread 0.00mm    PASS
-- home range --  PASS  homes from every distance
-- rail measure -- 299.00mm vs 299.00 claimed  MATCH
OVERALL: PASS (3 of 3 tests)
```

A failure stops the chain there and prints what to change, with numbers:
homing speed and its paired coolstep value, homing accel, homing current, and
mechanical last.

## Safety

**This drives the toolhead into the rail on purpose, repeatedly.** A threshold
that is too insensitive does not stop - it grinds until the axis runs out of
travel. Every test prints a warning and a countdown before moving. Stay on the
emergency stop.

**Y is never moved automatically.** Until the first successful home its
position is unmeasured, and moving an axis you cannot locate is how a gantry
ends up in a rail. Hand-place the toolhead at mid-travel before any Y test. X
is exempt - it is homed for real first.

## Driver support

| Generation | Drivers | Field | Range | Sensitivity |
|---|---|---|---|---|
| StallGuard2 | tmc2130, 2160, 2660, 5160 | `sgt` | -64..63 | **lower** = more sensitive |
| StallGuard4 | tmc2209 | `sgthrs` | 0..255 | **higher** = more sensitive |
| StallGuard4 | tmc2240 | `sg4_thrs` | 0..255 | **higher** = more sensitive |

Sweeps always start at the most sensitive end and walk toward the least, since
over-sensitive fails safely with an early trip while under-sensitive grinds.

## The rules that must hold first

If these are wrong, no threshold will ever work and sweeping is wasted effort.
`SENSORLESS_STATUS` checks them.

- **`homing_speed` must exceed `rotation_distance`.** StallGuard reads load
  from back-EMF; too slow and there is no signal to read.
- **`coolstep_threshold` must sit just below `homing_speed`.** It is the speed
  above which StallGuard is active. Too far below and StallGuard watches the
  acceleration ramp, and trips on acceleration load rather than on the rail.
- **The threshold is only valid at the current and speed it was tuned at.**
  Both scale back-EMF. Change either and re-sweep.

## On CoreXY, a passing axis exonerates the drivetrain

Both motors and both belts turn for *either* axis. So if X passes, the motors,
belts, pulleys and grub screws are all proven good, and the usual "check your
belt tension" advice is actively misleading for a Y failure. What Y does not
share with X is its own linear rails and the gantry mass it alone drags.

The tools apply this: once one axis has passed, a failure on the other says so
explicitly rather than sending you to check belts.

## What has actually been tested

Read this before running it. It has been used on **exactly one machine**, and
several things it supports are implemented from datasheets rather than proven
on hardware.

**Tested on:**

| | |
|---|---|
| Printer | Voron 2.4 r2, 300mm |
| Kinematics | **CoreXY only** |
| Board | BTT Kraken, TMC5160 on X and Y |
| Motors | LDO-42STH48-2004MAH, 0.9 degree, 2.0A peak |
| **Supply** | **48V rail on X and Y** - see the note below, this matters |
| Klipper | v0.13.0-743 |
| Host | Debian 13 (trixie), Python 3.13.5 |
| Moonraker | default, `localhost:7125` |

Settled on this machine: X `sgt=1` at homing_speed 78 / coolstep 65, Y `sgt=1`
at homing_speed 100 / coolstep 83, homing current 1.0A against run 1.2A /
hold 1.0A, `rotation_distance` 40 with 400 full steps per rotation.

**NOT tested:**

- **StallGuard4 hardware.** The tmc2209 (`sgthrs`) and tmc2240 (`sg4_thrs`)
  paths are written from the datasheet field definitions and the opposite
  sensitivity direction is handled, but no 2209 or 2240 has ever run this. Try
  it with your hand on the emergency stop and expect to find bugs.
- **Anything but CoreXY.** The travel maths is CoreXY-specific
  (`x = (a+b)/2`, `y = (a-b)/2`). On a cartesian or delta machine the measured
  distances will be wrong, and wrong measurements here mean the head is driven
  somewhere unexpected. Do not run it.
- **A hot machine, or an enclosed one.** This is the biggest gap, so read it
  properly.

  Every value here was found on a machine **with the side panels off and the
  chamber unheated** - effectively room temperature throughout. Nothing has
  been tuned or verified with the enclosure closed and the chamber at 50-60C.

  That matters because StallGuard drifts with motor temperature, and this
  repo's own data shows it: X measured a 0.00mm spread cold, then drifted to
  0.60mm across a single warm day, and recovered once the machine cooled. The
  design works around this by homing X and Y **cold** in `START_PRINT` and
  re-homing only Z after the heat soak, since Z uses a probe that does not
  care about temperature. But "works around" is not "verified".

  Y in particular is unproven: its 0.60mm spread was measured cold, and its
  working window is 2 values wide against X's 3 - so it has less margin to
  absorb any drift. Whether that holds hot is genuinely unknown.

  If you run this, **re-run `VERIFY_X_HOME RUNS=10` and `VERIFY_Y_HOME
  RUNS=10` after a long print**, with the machine at working temperature.
  That is the condition that actually decides whether a threshold is usable,
  and it costs a few minutes. A window one value wide will very likely fail
  that test.

  I will do this myself once the panels are on, and update the repo with what
  I find - good or bad.
- **Any supply voltage other than 48V.** X and Y run on a **48V rail** here,
  and that is not a detail - it is one of the main reasons a threshold from
  one machine will not transfer to another.

  Higher rail voltage drives current into the windings faster, so the motor
  holds torque to a higher speed and the load signature StallGuard reads is
  different. A 24V machine - which is the more common Voron build - will
  likely need a different threshold, and quite possibly a lower
  `homing_speed`, because it cannot sustain torque as far up the speed range.
  The `homing_speed` values here (78 for X, 100 for Y) may simply not be
  reachable usefully at 24V.

  So treat every number in this repo as a starting point for the sweep, not
  as a value to copy. That is what the sweep is for.

- **Any board other than the Kraken, or any other motor.** Current and motor
  characteristics both scale back-EMF, so a different motor changes what every
  threshold means. Note also that TMC2209 boards are typically 24V while these
  TMC5160s are on 48V, so the untested StallGuard4 path differs from the
  tested setup in *two* ways at once, not one.
- **Long-term use.** This was written and used over a few days. It has not run
  across firmware upgrades or seen a large sample of prints.

**It deliberately crashes your printer.** Every sweep drives the toolhead into
the rail on purpose, and a threshold that is too insensitive does not stop - it
grinds until the axis runs out of travel. On this machine a failed value
produced roughly 150mm of a motor fighting the stop. Belts can skip, printed
parts can crack, and a badly wrong config could do worse. Stay on the
emergency stop for the whole run.

No warranty, and no liability - see the disclaimer at the top and the MIT
licence at the bottom. If it breaks your machine, that is on you.

## Contributing

Improvements welcome - issues and pull requests both. This was written to
solve one machine's problem and then generalised, so there is plenty that
could be better.

Particularly useful:

- **StallGuard4 hardware reports.** The tmc2209 and tmc2240 paths are written
  from datasheets and have never been run. If you try it, say what happened
  either way - a "worked fine on a 2209" is as useful as a bug report.
- **Non-CoreXY kinematics.** The travel maths is CoreXY-specific and the tool
  should refuse to run elsewhere rather than measure nonsense. Someone who
  knows cartesian or delta step relationships could fix that properly.
- **Results from other machines** - motor, voltage, board, and the values you
  settled on. The more of those there are, the better the starting suggestions
  can get.
- **Temperature behaviour.** All tuning here was done cold, deliberately. If
  you have data on how much a threshold drifts as a chamber heats, that is the
  gap I most want filled.

No strong conventions - readable code and a note on what you tested is plenty.
If you find something wrong, saying so is a contribution too.

## License

MIT.
