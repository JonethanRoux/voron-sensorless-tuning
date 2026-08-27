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
| Motors | LDO-42STH48-2004MAH, 0.9 degree, 2.0A peak, 48V on XY |
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
- **A hot machine.** All tuning was done cold, with an unheated chamber. That
  is deliberate: StallGuard drifts with motor temperature, so the design homes
  X and Y cold and re-homes only Z after the heat soak. Values found cold may
  not hold at 60C chamber, and the tools do not compensate for temperature.
- **Any board other than the Kraken**, any motor other than the one above, and
  any voltage other than 48V on XY. Current and speed both scale back-EMF, so a
  different motor or supply voltage changes what every threshold means.
- **Long-term use.** This was written and used over a few days. It has not run
  across firmware upgrades or seen a large sample of prints.

**It deliberately crashes your printer.** Every sweep drives the toolhead into
the rail on purpose, and a threshold that is too insensitive does not stop - it
grinds until the axis runs out of travel. On this machine a failed value
produced roughly 150mm of a motor fighting the stop. Belts can skip, printed
parts can crack, and a badly wrong config could do worse. Stay on the
emergency stop for the whole run.

No warranty. See the licence. If it breaks your machine, that is on you.

## License

MIT.
