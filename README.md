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

## Install

Requires [gcode_shell_command](https://github.com/dw-0/kiauh) (KIAUH installs
it) and Moonraker on `localhost:7125`.

```bash
cp scripts/sgt_diag.py scripts/sgt_run.sh ~/
chmod +x ~/sgt_run.sh
cp config/sensorless*.cfg ~/printer_data/config/homing/
```

Add to `printer.cfg`:

```
[include homing/*.cfg]
```

Paths in `sgt_run.sh` and the `[gcode_shell_command]` blocks assume the user is
`voron24` - edit if yours differs. Then `FIRMWARE_RESTART`.

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

## Status

Working on a Voron 2.4 with TMC5160s. Tested on that machine only - the
StallGuard4 paths are implemented from the datasheet field definitions but have
not been run on real 2209/2240 hardware. Reports welcome.

## License

MIT.
