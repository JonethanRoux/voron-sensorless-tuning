#!/usr/bin/env python3
"""
Sensorless homing diagnostic for the Voron.

Sweeps driver_SGT for one axis, measuring the ACTUAL homing travel from raw MCU
step counts, and classifies every value: early-stop / reached-rail / no-trigger.
Also does repeat-run verification of a chosen value.

Why this is a script and NOT a Klipper macro:
  * a macro's gcode is rendered BEFORE it executes, so it cannot branch on a
    value it measured earlier in the same macro
  * a failed G28 aborts the whole macro, so a macro cannot record "no trigger"
    and continue to the next value
  * MCU step counts are only reachable via GET_POSITION console output

Usage:
    python3 ~/sgt_diag.py                  sweep X with defaults
    python3 ~/sgt_diag.py Y                sweep Y
    python3 ~/sgt_diag.py X -10 20 2       axis, from, to, step
    python3 ~/sgt_diag.py X --verify 0 5   5 repeat runs at SGT=0
"""
import json
import re
import sys
import time
import urllib.parse
import urllib.request

BASE = 'http://localhost:7125'


def post(script, timeout=300):
    # quote(), not a naive space swap: RESPOND text contains - > % ( ) etc
    try:
        urllib.request.urlopen(urllib.request.Request(
            BASE + '/printer/gcode/script?script=' + urllib.parse.quote(script),
            method='POST'), timeout=timeout).read()
    except urllib.error.HTTPError as e:
        # Moonraker reports the status line ('400: Unknown') and drops Klipper's
        # actual complaint into the body, so an unread body turns every real
        # fault into a useless message. Read it and carry it on the exception -
        # still an HTTPError, so callers that treat one as 'no trigger' are
        # unaffected.
        try:
            body = e.read().decode('utf-8', 'replace')[:400]
        except Exception:
            body = ''
        if body:
            e.msg = '%s | klipper said: %s' % (e.msg, body)
            print('  [gcode error] %s -> %s' % (script[:60], body))
            sys.stdout.flush()
        raise


# Set once X has actually homed. On CoreXY an X move turns BOTH motors through
# BOTH belts, so a passing X is hard evidence that the motors, belts, pulleys and
# grub screws are all sound - and it makes the stock "check your belts" advice
# actively misleading for a Y failure. Only the parts Y does NOT share with X can
# still be at fault: the Y linear rails, and the gantry it alone has to drag.
X_PROVEN = False

SUMMARY_FILE = '/home/voron24/printer_data/logs/sgt_summary.txt'
SUMMARY = []


def note(line=''):
    """Collect a line for the final summary block.

    Klipper echoes every GET_POSITION (a 7-line dump) and every
    SET_TMC_CURRENT, and neither can be silenced - they are Klipper
    responding, not us. During a run that noise buries anything we print.
    So findings are ALSO collected here and posted as one block at the very
    end, after all motion, where nothing can scroll them away.
    """
    SUMMARY.append(line)


def flush_summary(title):
    """Post the collected findings as one clean block, and save it so
    SHOW_SGT_RESULT can recall it later without re-running anything."""
    # Mainsail's console panel is narrow. Anything past ~42 characters wraps
    # onto a second line and the block looks broken, so keep every line short.
    # 40 chars max per line: Mainsail's console panel wraps beyond about 42.
    # This is posted AFTER M84, so no further motion output can push it up -
    # it is always the last thing in the console.
    bar = '-' * 40
    passes = len([l for l in SUMMARY if l.startswith(('PASS', 'MATCH'))])
    fails = len([l for l in SUMMARY if l.startswith(('FAIL', 'MARGINAL'))])
    if fails:
        overall = 'OVERALL: FAIL  (%d passed, %d failed)' % (passes, fails)
    elif passes:
        overall = 'OVERALL: PASS  (%d of %d tests)' % (passes, passes)
    else:
        overall = 'OVERALL: no result'
    block = [bar, title, bar] + SUMMARY + [bar, overall, bar]
    try:
        with open(SUMMARY_FILE, 'w') as f:
            f.write('\n'.join(block) + '\n')
    except Exception:
        pass
    print('')
    for line in block:
        print(line)
        sys.stdout.flush()
        try:
            post('RESPOND MSG="%s"' % line.replace('"', "'"), timeout=20)
        except Exception:
            pass


CONFIG_FILE = '/home/voron24/printer_data/config/printer.cfg'


def apply_config(rig, value):
    """Write the tuned threshold into printer.cfg, inside the correct driver
    section only.

    Returns (changed, message). Backs up first - this edits the file that
    decides how the machine moves, so it must be trivially reversible.

    The edit is bounded to the driver's own section: printer.cfg contains a
    driver_SGT for stepper_x AND stepper_y, so a naive whole-file replace would
    silently overwrite the other axis.
    """
    key = 'driver_' + rig.field.upper()
    try:
        text = open(CONFIG_FILE).read()
    except Exception as e:
        return False, 'could not read printer.cfg: %s' % e

    head = '[' + rig.tmc_sec + ']'
    if head not in text:
        return False, 'section %s not found' % head
    start = text.index(head)
    nxt = text.find('\n[', start + 1)
    end = nxt if nxt != -1 else len(text)
    block = text[start:end]

    import re as _re
    cur = _re.search(r'^%s:\s*(-?\d+)' % _re.escape(key), block, flags=_re.M)
    if cur and int(cur.group(1)) == int(value):
        return False, '%s already %d - no change, no restart needed' % (key, value)

    backup = CONFIG_FILE + '.sgt.bak'
    try:
        open(backup, 'w').write(text)
    except Exception as e:
        return False, 'backup failed, not touching the config: %s' % e

    if cur:
        newblock = _re.sub(r'^%s:\s*-?\d+' % _re.escape(key),
                           '%s: %d' % (key, value), block, count=1, flags=_re.M)
    else:
        lines = block.rstrip('\n').split('\n')
        lines.append('%s: %d' % (key, value))
        newblock = '\n'.join(lines) + '\n'
    try:
        open(CONFIG_FILE, 'w').write(text[:start] + newblock + text[end:])
    except Exception as e:
        return False, 'write failed: %s' % e
    return True, '%s: %d written to printer.cfg' % (key, value)


def say(msg=''):
    """Print to the log AND push into the Mainsail console.

    The script runs detached, so plain print() only reaches the log file. The
    console meanwhile fills with the GET_POSITION dumps this script issues to
    read step counts - all noise and no findings. RESPOND puts the actual
    results where they are being read.
    """
    print(msg)
    sys.stdout.flush()
    try:
        clean = msg.replace('"', "'")
        if clean.strip():
            post('RESPOND MSG="%s"' % clean, timeout=20)
    except Exception:
        pass    # console output is a convenience, never fail a test over it


def query(objs):
    # Encode, for the same reason post() does. Object names such as
    # 'gcode_macro _SENSORLESS_VARS' contain a space, and http.client rejects a
    # raw space in a URL outright - InvalidURL, before the request is even sent.
    # safe='&=' keeps multi-object queries ('toolhead&configfile') working.
    with urllib.request.urlopen(
            BASE + '/printer/objects/query?' + urllib.parse.quote(objs, safe='&='),
            timeout=15) as r:
        return json.load(r)['result']['status']


def mcu_steps(timeout=6.0):
    """Raw MCU step counters, guaranteed FRESH.

    This used to post GET_POSITION, sleep a fixed second, then take the newest
    'mcu:' line in the gcode store. When the response had not landed yet it
    silently returned the PREVIOUS reading, so a measurement was computed
    against a baseline from an earlier moment. On a rail measurement that made
    the start counter identical run after run while the end counter advanced,
    adding a clean 640 steps - exactly 2.00mm - to every successive result. The
    numbers looked plausible and drifted monotonically, which is the worst way
    for a measurement bug to present.

    So: note the newest store timestamp BEFORE asking, then poll until an
    'mcu:' line NEWER than that appears. If none arrives, raise rather than
    return something stale - a wrong number here silently corrupts every
    distance the tool reports.
    """
    def newest_time():
        try:
            with urllib.request.urlopen(BASE + '/server/gcode_store?count=1',
                                        timeout=10) as r:
                st = json.load(r)['result']['gcode_store']
                return st[-1]['time'] if st else 0.0
        except Exception:
            return 0.0

    mark = newest_time()
    post('GET_POSITION')
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.15)
        try:
            with urllib.request.urlopen(BASE + '/server/gcode_store?count=25',
                                        timeout=10) as r:
                store = json.load(r)['result']['gcode_store']
        except Exception:
            continue
        for m in reversed(store):
            if m.get('time', 0) <= mark:
                continue            # older than our request - not our answer
            hit = re.search(r'mcu:\s*(.*)', m['message'])
            if hit:
                d = dict(re.findall(r'(stepper_\w+):(-?\d+)', hit.group(1)))
                if 'stepper_x' in d:
                    return int(d['stepper_x']), int(d['stepper_y'])
    raise RuntimeError('GET_POSITION did not return a fresh mcu line in %.0fs'
                       % timeout)


SG_SPEC = {
    'tmc2130': ('sgt',      -64, 63,  -1),
    'tmc2660': ('sgt',      -64, 63,  -1),
    'tmc5160': ('sgt',      -64, 63,  -1),
    'tmc2209': ('sgthrs',     0, 255,  1),
    'tmc2240': ('sg4_thrs',   0, 255,  1),
}


def detect_driver(axis):
    """Find which tmc driver section drives this axis. Returns
    (section_name, driver_type, field, lo, hi, sign) where sign is +1 when a
    HIGHER value is MORE sensitive."""
    objs = query_list()
    want = 'stepper_' + axis.lower()
    for o in objs:
        if ' ' in o and o.split()[0] in SG_SPEC and o.split()[1] == want:
            drv = o.split()[0]
            field, lo, hi, sign = SG_SPEC[drv]
            if drv == 'tmc2240':
                st = query('configfile')['configfile']['settings'][o]
                if not st.get('driver_sg4_thrs'):
                    field, lo, hi, sign = 'sgt', -64, 63, -1
            return o, drv, field, lo, hi, sign
    raise RuntimeError('no supported TMC driver found for ' + want)


def query_list():
    with urllib.request.urlopen(BASE + '/printer/objects/list', timeout=15) as r:
        return json.load(r)['result']['objects']


class Rig(object):
    def __init__(self, axis):
        self.axis = axis.upper()
        s = query('configfile')['configfile']['settings']
        st = s['stepper_' + self.axis.lower()]
        (self.tmc_sec, self.drv, self.field,
         self.sg_lo, self.sg_hi, self.sg_sign) = detect_driver(self.axis)
        tmc = s[self.tmc_sec]
        v = s['gcode_macro _sensorless_vars']
        self.step_dist = st['rotation_distance'] / float(
            st['full_steps_per_rotation'] * st['microsteps'])
        self.rot_dist = st['rotation_distance']
        self.unload = float(v['variable_unload_dist'])
        self.backoff = float(v['variable_backoff'])
        self.pos_max = st['position_max']
        self.pos_min = st['position_min']
        _sx = query('configfile')['configfile']['settings']['stepper_x']
        self.mid_x = (_sx['position_min'] + _sx['position_max']) / 2.0
        # Sweeps always start at the MOST sensitive end and walk toward the
        # least, because over-sensitive fails safe (an early trip) while
        # under-sensitive grinds the rail. Which numeric end that is depends on
        # the driver: StallGuard2 (tmc2130/2660/5160) counts DOWN to more
        # sensitive, StallGuard4 (tmc2209/2240) counts UP.
        if self.sg_sign < 0:
            self.sg_start, self.sg_end = self.sg_lo, self.sg_hi
        else:
            self.sg_start, self.sg_end = self.sg_hi, self.sg_lo
        self.speed = st['homing_speed']
        self.cool = tmc.get('coolstep_threshold')
        # Live macro state, not configfile: SET_GCODE_VARIABLE changes the
        # running value and never touches the file, so configfile would report
        # a current the driver is not actually using.
        live = query('gcode_macro _SENSORLESS_VARS')['gcode_macro _SENSORLESS_VARS']
        base = float(live.get('home_current', v['variable_home_current']))
        yhc = float(live.get('y_home_current', 0) or 0)
        self.current = yhc if (self.axis == 'Y' and yhc > 0) else base
        self.sgt_cfg = tmc.get('driver_' + self.field, tmc.get('driver_sgt', 0))
        self.expected = self.pos_max / 2.0
        # Are we sitting AT the rail? Only true right after a home that got
        # there. goto_start() must never back off blindly - from mid-gantry a
        # 150mm back-off drives straight into the opposite rail.
        self.at_rail = False
        ##  at_rail only says a home RETURNED. frame_ok says the resulting
        ##  coordinate was actually reconciled against the step counters.
        self.frame_ok = False

    def banner(self):
        say('axis %s   driver %s   field %s   range %d..%d   %s = more sensitive'
              % (self.axis, self.drv, self.field, self.sg_lo, self.sg_hi,
                 'LOWER' if self.sg_sign < 0 else 'HIGHER'))
        say('  homing_speed=%s  coolstep_threshold=%s  home_current=%s  config %s=%s'
              % (self.speed, self.cool, self.current, self.field, self.sgt_cfg))
        if self.cool and self.speed <= self.cool:
            say('  !! homing_speed must be ABOVE coolstep_threshold or it can never trigger')
        if self.speed <= self.rot_dist:
            say('  !! klipper_tmc_autotune: homing_speed should EXCEED rotation_distance (%s)'
                  % self.rot_dist)
        say('')

    def set_sgt(self, val):
        post('SET_TMC_FIELD STEPPER=stepper_%s FIELD=%s VALUE=%d'
             % (self.axis.lower(), self.field, val))
        time.sleep(0.6)

    def ensure_frame(self):
        """Make BOTH axes count as homed so G1 is usable.

        On CoreXY there is no X motor and no Y motor - both belts are driven by
        both motors, so ANY single-axis move turns both. G1 drives them
        simultaneously and stays on-axis. FORCE_MOVE can only drive one stepper
        per command, so using it for a two-stepper move gives two 45 degree legs:
        the head swings far off-axis and back. That is the diagonal you see.

        The axis under test is homed for real. When X is under test, Y is merely
        DECLARED at mid-travel - it is never measured, it just has to exist so G1
        will run. When Y is under test the reverse is NOT safe: X is homed for
        real, because a declared X corrupts the frame every Y move depends on.
        """
        other = 'y' if self.axis == 'X' else 'x'
        if other not in query('toolhead')['toolhead']['homed_axes']:
            if self.axis == 'Y':
                # Declaring X here would tell the machine the head sits at
                # mid-travel while it physically sits somewhere else. On CoreXY
                # every following Y move is computed from that wrong frame, so
                # the gantry drives into the rail. X is tuned by now: home it.
                post('G28 X')
                time.sleep(0.5)
            else:
                st = query('configfile')['configfile']['settings']['stepper_' + other]
                post('SET_KINEMATIC_POSITION %s=%.0f'
                     % (other.upper(), st['position_max'] / 2.0))
                time.sleep(0.3)

    def goto_start(self):
        """Back off a FIXED distance from the rail before each trial.

        This briefly retraced the exact distance the previous home travelled, to
        start every trial from the identical spot. That is right when the
        previous home was a full-length one - and wrong the moment it was not.
        A sweep ends on a grind with the head AT the rail, so retracing returns
        it to the rail, the next home trips after a few millimetres, and the run
        after that starts from there too. The repeatability test then measures
        ten 8mm twitches instead of ten 150mm approaches and calls the axis
        broken.

        The rail is the only stable reference on the axis, so the start line is
        measured from it. correct_frame() has already reconciled Klipper's
        position with the step counters - after a real home and after a false
        trigger alike - so an absolute move is trustworthy and lands the same
        place every time, which is exactly what the measurement needs.
        """
        if self.at_rail and not self.frame_ok:
            say('    (not repositioning - frame untrusted, homing from here)')
        if not self.at_rail or not self.frame_ok:
            ##  at_rail is True after ANY home that returned, a false trigger
            ##  included, so on its own it is no evidence this coordinate means
            ##  anything. Homing from wherever we are is worse for the
            ##  measurement but cannot crash; an absolute move on a bad frame can.
            return False
        self.ensure_frame()
        post('G90')
        post('G1 %s%.1f F6000' % (self.axis, self.pos_max - self.expected))
        post('M400')
        time.sleep(0.5)
        return True

    def one_home(self):
        """Home once. Returns (triggered, travel_mm).
        Reconciles the coordinate frame afterwards so a false trigger cannot
        leave Klipper believing the axis is somewhere it is not."""
        if self.axis == 'Y':
            # Fresh X reference before EVERY Y attempt, not just the first.
            # A Y trial that grinds the rail can rack the gantry in X, and on
            # CoreXY every Y move is resolved through the X frame - so a stale
            # X quietly turns the next measurement into fiction. Re-homing X
            # costs a few seconds and removes the whole failure mode.
            try:
                post('G28 X')
                time.sleep(0.3)
                # Homing parks X hard against its rail. Leaving it there puts the
                # gantry in a corner for every Y trial, and X visibly slams to the
                # rail between attempts. Put it back to mid-travel so both rails
                # have room and the Y measurement is not taken from an extreme of
                # the X travel.
                post('G90')
                post('G1 X%.1f F6000' % self.mid_x)
                post('M400')
                time.sleep(0.3)
            except urllib.error.HTTPError:
                pass
        # Klipper reports 0 for an UNHOMED axis, which is not where the head is.
        # Correcting the frame from that invents a position - it once told
        # Klipper the head was at 2.2 while it sat against the rail at 300, so
        # the next absolute move drove straight into that rail and every trial
        # afterwards measured a 12mm twitch with a perfect 0.00mm spread.
        # Without a valid starting point, the post-home position_max is the only
        # thing we know, so leave it alone.
        homed_before = self.axis.lower() in query('toolhead')['toolhead']['homed_axes']
        p0 = axis_pos(self.axis) if homed_before else None
        ##  Trust PROPAGATES. If we knew where the head was before this home,
        ##  then p0 + measured displacement is still known afterwards - even
        ##  after a false trigger. The earlier bug was not that p0 was used, it
        ##  was that p0 was used WITHOUT knowing whether it meant anything.
        trusted_before = bool(self.frame_ok and p0 is not None)
        ##  How far the rail REALLY is, when we are entitled to say. classify()
        ##  needs this rather than a fixed assumption, because the head no
        ##  longer returns to the same start line before every trial.
        self.last_to_rail = (self.pos_max - p0) if trusted_before else None
        a0, b0 = mcu_steps()
        try:
            post('G28 ' + self.axis)
            ok = True
        except urllib.error.HTTPError:
            ok = False
        time.sleep(0.6)
        a1, b1 = mcu_steps()
        da, db = a1 - a0, b1 - b0
        self.at_rail = ok
        # CoreXY:  x = (a + b) / 2   ,   y = (a - b) / 2
        raw = (da + db) / 2.0 if self.axis == 'X' else (da - db) / 2.0
        # The macro's pre-home unload only fires when the axis is already homed
        # AND within unload_dist of the rail - never true after a big back-off -
        # so only the post-home backoff is added here.
        travel = raw * self.step_dist + self.backoff
        # Net physical displacement of THIS attempt. Kept so the head can be
        # retraced to precisely where it started, rather than sent to a fixed
        # coordinate that any frame error would shift.
        self.last_moved = raw * self.step_dist
        self.start_pos = p0
        ##  Anchor from the RAIL, never from p0.
        ##
        ##  correct_frame() computed the position as p0 + moved. When p0 was
        ##  itself fiction - an earlier home that false-triggered and left
        ##  Klipper believing position_max - the "correction" inherited that
        ##  error and looked entirely plausible. The head then sat ~145mm from
        ##  where the frame claimed, and the next absolute move drove it into
        ##  the opposite rail. Validating that a correction RAN is not the same
        ##  as validating its input.
        ##
        ##  travel comes only from step counters, so it needs no reference: a
        ##  home that covered the expected distance genuinely reached the rail,
        ##  and its endpoint is exactly position_max - a hard physical fact.
        ##  Anything shorter means the absolute position is UNKNOWABLE, and the
        ##  honest response is to refuse absolute moves rather than guess one.
        ##  Two cases, and conflating them is what produced ten 10mm twitches
        ##  where 150mm approaches were reported.
        ##
        ##  A short travel does NOT mean "did not reach the rail". Starting AT
        ##  the rail and homing gives a short travel precisely because you are
        ##  already there. Requiring a long travel to trust the frame threw away
        ##  a perfectly good anchor every time the previous trial ground.
        if trusted_before:
            ##  p0 was real, so everything here is real. Compare what the home
            ##  covered against how far the rail actually was.
            to_rail = self.pos_max - p0
            tol = max(3.0, abs(to_rail) * 0.05)
            if to_rail > 0 and abs(travel - to_rail) <= tol:
                at = self.pos_max - self.backoff      # genuine contact
            else:
                at = max(self.pos_min,
                         min(self.pos_max, p0 + self.last_moved))
                say('    false trigger: %.1fmm short of the rail, frame'
                    ' corrected to %.1f' % (to_rail - travel, at))
            post('SET_KINEMATIC_POSITION %s=%.3f' % (self.axis, at))
            time.sleep(0.25)
            self.frame_ok = True
        elif travel >= self.expected * 0.90:
            ##  No usable p0, so only a long travel proves rail contact. A home
            ##  that TRIGGERED ran the macro's backoff; one that ground did not.
            post('SET_KINEMATIC_POSITION %s=%.3f'
                 % (self.axis, self.pos_max - (self.backoff if ok else 0.0)))
            time.sleep(0.25)
            self.frame_ok = True
            say('    frame re-anchored from the rail')
        else:
            self.frame_ok = False
            say('    position UNKNOWABLE - no trusted reference and no rail'
                ' contact. Absolute moves suspended.')
        post('M400')
        time.sleep(0.4)
        return ok, travel

    def less_sensitive(self):
        """Direction that makes the threshold LESS sensitive.
        SG2 (sgt): higher = less sensitive. SG4 (sgthrs): lower = less."""
        return 'RAISE' if self.sg_sign < 0 else 'LOWER'

    def more_sensitive(self):
        return 'LOWER' if self.sg_sign < 0 else 'RAISE'

    def classify(self, ok, travel):
        """GOOD / EARLY / SHORT / FAIL for one homing attempt.

        This used to call anything past expected * 1.25 a FAIL, on the
        assumption that every trial starts the same distance from the rail.
        That held while goto_start repositioned before each one. It stopped
        holding when goto_start began - correctly - refusing to move on a frame
        it could not verify: after four false triggers the head sits ~32mm
        further out, and a perfectly good home then covers 210mm and was
        reported as a grind. A real sweep declared NOTHING WORKED on an axis
        whose window had already been measured three times.

        A grind does not need to be inferred from distance. StallGuard either
        fired or it did not, and when it does not Klipper errors out - which is
        exactly what `ok` carries. Travel only has to separate a trigger that
        happened at the rail from one that happened on the way there, and for
        that the reference is how far the rail ACTUALLY was, not how far it is
        assumed to be.
        """
        if not ok:
            return 'FAIL'           # never triggered: it ground the rail
        if travel < 10:
            return 'EARLY'          # tripped essentially on the spot
        ##  last_to_rail is the real distance when the frame was trusted going
        ##  in; otherwise fall back to the nominal mid-axis assumption.
        ref = getattr(self, 'last_to_rail', None) or self.expected
        if travel >= ref * 0.90:
            return 'GOOD'
        return 'SHORT'

    def explain(self, verdict):
        if verdict == 'GOOD':
            return 'reached the rail - this value WORKS'
        if verdict == 'FAIL':
            return 'never triggered, ground the rail -> %s %s (more sensitive)' % (
                self.more_sensitive(), self.field)
        if verdict == 'EARLY':
            return 'tripped instantly -> %s %s (less sensitive)' % (
                self.less_sensitive(), self.field)
        return 'stopped part way -> %s %s slightly' % (self.less_sensitive(), self.field)


def sweep(rig, lo, hi, step, chain=True):
    rig.banner()
    say('  travel measured from MCU step counts. Wall-clock is useless: the homing')
    say('  macro contains seconds of fixed dwell that swamp any timing.')
    say('  Start the head mid-axis. A correct home travels about %.0fmm.' % rig.expected)
    say()
    ##  A sweep exists to PRODUCE false triggers, and both runway helpers do a
    ##  blind relative G1 that cannot stop on contact. With second_home_dist=40
    ##  every trial ran G28, G1 -40, G28, G1 -10 - about -47mm net per trial -
    ##  which walked the head 150 -> 103 -> 56 -> 9 and into the front rail on
    ##  the fourth. The second home is only safe when triggers are trustworthy,
    ##  which during a threshold sweep is exactly what they are not.
    _v = query('gcode_macro _SENSORLESS_VARS')['gcode_macro _SENSORLESS_VARS']
    _was_second = _v.get('second_home_dist', 0) or 0
    _was_unload = _v.get('unload_dist', 0) or 0
    if float(_was_second) or float(_was_unload):
        say('  suppressing runway moves for the sweep '
            '(second_home_dist %s, unload_dist %s -> 0)' % (_was_second, _was_unload))
        set_var('second_home_dist', 0)
        set_var('unload_dist', 0)
    good = []
    ceiling = None          # the value that closed the window, if we found it
    rig.trail = []          # (value, verdict) for every trial, in order
    val = lo
    known = False
    ##  Cumulative drift since the last REAL rail contact. Every false trigger
    ##  nets about -8mm (a short trip minus the 10mm backoff), and with
    ##  goto_start correctly refusing to reposition on an untrusted frame there
    ##  is nothing to put the head back. Thirty trials of that walks it the
    ##  length of the axis. Per-trial checks cannot see a drift this gradual.
    drift = 0.0
    while (val <= hi if step > 0 else val >= hi):
        rig.set_sgt(val)
        rig.goto_start()
        known = True
        ok, travel = rig.one_home()
        verdict = rig.classify(ok, travel)
        rig.trail.append((val, verdict))
        say('  %s=%4d  travel %7.1fmm  [%-5s] %s'
              % (rig.field, val, travel, verdict, rig.explain(verdict)))
        sys.stdout.flush()
        if verdict == 'GOOD':
            good.append(val)
            say('        -> working values so far: %s' % good)
            sys.stdout.flush()
        note('%s=%-4d %7.1fmm  %s' % (rig.field, val, travel, verdict))
        ##  A real rail contact re-anchors everything; otherwise accumulate.
        if rig.frame_ok:
            drift = 0.0
        else:
            drift += rig.last_moved
            if drift < -(rig.expected * 0.5):
                say()
                say('  ABORTING - the head has crept %.0fmm from the rail across'
                    ' %d trials' % (drift, len(rig.trail)))
                say('  without a single real contact to re-anchor it. Every')
                say('  value tried so far is too sensitive. Restart the sweep')
                say('  from a LESS sensitive value than %d.' % val)
                break

        ##  Backstop. If a trial carried the head AWAY from the rail, something
        ##  is walking it toward the opposite end and the next trials will keep
        ##  going. Catch it on the first one rather than the fourth.
        if rig.last_moved < -20.0:
            say()
            say('  ABORTING - that trial moved %.1fmm AWAY from the rail.'
                % rig.last_moved)
            say('  Something is walking the head toward the opposite end.')
            say('  Check second_home_dist and unload_dist before re-running.')
            break
        if verdict == 'FAIL':
            ceiling = val
            ##  A grind proves the head is hard against the rail, so a RELATIVE
            ##  move away from it is safe with no coordinate frame at all. This
            ##  leaves the axis parked mid-travel instead of jammed, which is
            ##  what the chained repeatability test needs - and saves having to
            ##  re-centre by hand between the sweep and the verify.
            ##  There WAS a blind relative backoff here - G1 -150 - argued safe
            ##  because "a grind proves the head is against the rail". It does
            ##  not. It proves the CLASSIFIER said so, and the classifier was
            ##  wrong: it called a genuine 210mm home a grind because the head
            ##  had drifted further from the rail than assumed. Backing off
            ##  150mm from mid-axis then drove the head into the opposite end.
            ##
            ##  The move was never needed. The gate below releases the motors
            ##  and asks the operator to square and centre by hand anyway, so
            ##  the tool was taking a risk to save a step it does not save.
            ##  When in doubt, do not move: ask.
            ##  A grind does not only cost the axis being swept. On CoreXY the
            ##  carriage is blocked while both motors keep stepping, so A and B
            ##  skip by DIFFERENT amounts - and since y = (a - b) / 2, that
            ##  difference is a real movement of the OTHER axis. The belts also
            ##  redistribute tension against a blocked carriage and drag the
            ##  gantry out of square.
            ##
            ##  Warning about that and then measuring anyway is worse than not
            ##  warning: the repeatability runs that follow describe the skew
            ##  rather than the threshold. One such run reported a 197mm
            ##  "spread", which is not a number any threshold can produce, and
            ##  on that basis it would have discarded a good value.
            ##
            ##  Homing the other axis drives the gantry into both its rails,
            ##  which is what physically squares a CoreXY gantry. Do it at the
            ##  KNOWN-GOOD config threshold, never the value that just ground.
            other = 'Y' if rig.axis == 'X' else 'X'
            rig.set_sgt(rig.sgt_cfg)
            say()
            say('  that grind can rack the gantry in %s.' % other)
            ##  The tool does NOT re-home to fix this, and that is deliberate.
            ##  G28 on the other axis is still a MOVE - on an axis whose
            ##  position is now unknown and whose own homing is unproven,
            ##  immediately after a crash. That is the exact state in which
            ##  this tool has already driven a gantry into a rail twice.
            ##
            ##  Squaring a racked gantry is a thirty-second job by hand and an
            ##  unbounded risk by script. Release the motors and ask.
            rig.frame_ok = False
            if chain:
                ok_go = ask_operator(
                    'Square the gantry before measuring',
                    ['sgt=%d ground the rail. On CoreXY that can rack the' % val,
                     'gantry: the carriage is blocked while both motors keep',
                     'stepping, and unequal skid between them moves %s.' % other,
                     'Anything measured on a skewed gantry describes the skew,',
                     'not the threshold.',
                     'Motors are OFF. Square the gantry by hand, then centre',
                     'the head on %s.' % rig.axis,
                     'Continue runs %d repeatability tests.' % len(good),
                     'Cancel keeps the window result above and stops.'])
                if not ok_go:
                    chain = False
            say('  STOPPING - past this point it only grinds the rail.')
            break
        val += step
    ##  Restore before any chained verify, so repeatability is measured against
    ##  the machine's real homing behaviour rather than the sweep's.
    if float(_was_second) or float(_was_unload):
        set_var('second_home_dist', _was_second)
        set_var('unload_dist', _was_unload)
        say('  runway moves restored (second_home_dist %s, unload_dist %s)'
            % (_was_second, _was_unload))
    say()
    say('  ' + '-' * 68)
    if good:
        ##  Which value to use was decided by heuristic - take the middle of the
        ##  window - on the reasoning that the middle has the most margin either
        ##  side. That is a fair prior, but it is only a prior: it never checked
        ##  whether the middle actually repeats best. Measure instead.
        ##
        ##  Every value here already reaches the rail, so this costs a handful
        ##  of ordinary homes and no grinding at all.
        pick = good[len(good) // 2]
        if len(good) > 1 and chain:
            say()
            say('  === repeatability on each of the %d working values ==='
                % len(good))
            say('  every value below reaches the rail; this decides which one')
            say('  lands in the SAME PLACE, which is what actually prints.')
            ##  The value that closed the window is NOT tested.
            ##
            ##  It was, briefly. The idea was that one grind does not prove a
            ##  value always grinds, so testing it might widen the window by
            ##  one. The cost is up to five more grinds, each able to rack the
            ##  gantry - and the result cannot be used either way, because a
            ##  ceiling value can never be the chosen threshold: it sits ON the
            ##  edge with no margin at all on the side that grinds. Five grinds
            ##  for a number nothing acts on is not a trade worth offering.
            todo = list(good)
            scored = []
            for n_v, v in enumerate(todo, 1):
                grinder = False
                ##  Gate before EVERY value, not only after the grind. The
                ##  gantry can shift between tests as easily as during one, and
                ##  five homes on a gantry that moved describe the movement.
                lines = ['About to run 5 repeatability homes at %s=%d.'
                         % (rig.field, v),
                         'Check the gantry is square, then centre the head on %s.'
                         % rig.axis,
                         'Motors are OFF.']
                lines += ['This value reached the rail during the sweep.']
                lines += ['Continue tests it. Cancel stops and keeps every',
                          'result measured so far.']
                if not ask_operator('Test %s=%d  (%d of %d)'
                                    % (rig.field, v, n_v, len(todo)), lines):
                    say('  stopped by operator - keeping %d result(s)'
                        % len([x for x in scored if x[1] is not None]))
                    break
                rig.frame_ok = False   # motors were off; the next home re-anchors
                say()
                say('  [%d/%d] %s=%d - 5 homes ...'
                    % (n_v, len(todo), rig.field, v))
                mark = len(SUMMARY)
                try:
                    verify(rig, v, 5, chain=False)
                    sp = getattr(rig, 'last_spread', None)
                except Exception as exc:
                    say('  %s=%d repeatability failed: %s' % (rig.field, v, exc))
                    sp = None
                del SUMMARY[mark:]
                scored.append((v, sp))
                ##  Everything worth reading goes HERE, after the runs. Put it
                ##  before and it scrolls past behind the per-run lines and the
                ##  number you actually came for arrives on its own.
                where = ('CEILING - the value that closed the window'
                         if v == ceiling else
                         'MOST SENSITIVE of the working values - nearest the'
                         ' false-trigger edge' if v == min(good) else
                         'LEAST SENSITIVE of the working values - nearest the'
                         ' grinding edge' if v == max(good) else
                         'mid-window - margin on both sides')
                say()
                say('  ' + '=' * 60)
                if sp is None:
                    say('  RESULT  %s=%d : NO RESULT - treated as unusable'
                        % (rig.field, v))
                else:
                    say('  RESULT  %s=%d : spread %.2fmm  (%s)'
                        % (rig.field, v, sp,
                           'excellent' if sp < 0.35 else
                           'usable' if sp < 1.0 else
                           'TOO LOOSE - this prints as a layer shift'))
                say('  WHERE   %s' % where)
                say('  MEANS   all five homes must land on the SAME number.')
                say('          The number itself only has to be about %.0fmm.'
                    % rig.expected)
                done = [x for x in scored if x[1] is not None]
                if len(done) > 1:
                    b = min(done, key=lambda x: x[1])
                    say('  BEST    so far %s=%d at %.2fmm  (%d of %d tested)'
                        % (rig.field, b[0], b[1], len(scored), len(todo)))
                say('  ' + '=' * 60)
            ##  A ceiling value can post a fine spread and still be the wrong
            ##  answer: it sits ON the edge, with no margin at all on the side
            ##  that grinds. Measured for information, never chosen.
            usable = [(v, sp) for v, sp in scored
                      if sp is not None and v in good]
            if usable:
                ##  Rank by measured spread. Ties break toward the LESS sensitive
                ##  end: both ends of the window fail as the machine heats, but
                ##  the sensitive end fails as a FALSE TRIGGER, which reports
                ##  success and corrupts the coordinate frame, while the
                ##  insensitive end merely grinds and tells you so.
                best_sp = min(sp for _v, sp in usable)
                tied = [v for v, sp in usable if sp <= best_sp + 0.05]
                pick = max(tied) if rig.sg_sign < 0 else min(tied)
                note('')
                note('-- repeatability by threshold --')
                for v, sp in scored:
                    note('%s=%-4d spread %s%s'
                         % (rig.field, v,
                            ('%.2fmm' % sp) if sp is not None else 'n/a',
                            '   <- chosen' if v == pick else ''))
                if len(tied) > 1:
                    note('tie on spread - took the less sensitive end, which')
                    note('fails by grinding rather than by lying about position')
        note('PASS  reached the rail at %s' % good)
        note('best  %s = %d' % (rig.field, pick))
        if len(good) == 1:
            note('WARN  window is 1 value wide - fragile')
            note('      one integer is not margin. It works cold and')
            note('      fails warm, as X did across a single day.')
            failure_advice(rig.axis, rig,
                           'one integer is not margin - it works cold and'
                           ' fails warm.')
        if chain:
            note('')
            say()
            say('  passed - chaining to repeatability (10 runs)')
            verify(rig, pick, 10, chain=chain)
            return good
        note('NEXT  VERIFY_%s_HOME RUNS=5' % rig.axis)
        say('  RESULT: %d value%s reached the rail: %s'
              % (len(good), '' if len(good) == 1 else 's', good))
        say()
        say('  USE THIS ->  driver_%s: %d' % (rig.field.upper(), pick))
        say('               in [%s] of printer.cfg' % rig.tmc_sec)
        if len(good) == 1:
            say()
            say('  WARNING: the window is ONE value wide. That is fragile - StallGuard')
            say('  drifts with motor temperature, so it can stop working as the machine')
            say('  soaks. Make sure START_PRINT homes BEFORE the heat soak.')
        say()
        say('  NEXT STEP - prove it repeats. One success can be a FALSE trigger,')
        say('  which reports success while stopping nowhere near the rail:')
        say('      VERIFY_%s_HOME RUNS=5' % rig.axis)
        say('  then if the spread is under 1mm:')
        say('      TEST_%s_HOME_RANGE     homes from 5..250mm off the rail' % rig.axis)
        say('      MEASURE_%s_RAIL        checks position_max is honest' % rig.axis)
    else:
        note('FAIL  no %s value reached the rail' % rig.field)
        note('the threshold is NOT the problem - every value was')
        note('tried and none worked, so stop tuning it')
        failure_advice(rig.axis, rig, 'no threshold worked at these settings.')
        if rig.speed <= rig.rot_dist:
            note('DO    homing_speed %g -> %g' % (rig.speed, rig.rot_dist * 1.5))
            note('      must exceed rotation_distance %g' % rig.rot_dist)
        else:
            note('DO    homing_speed %g, coolstep %g'
                 % (rig.speed * 1.3, rig.speed * 1.3 * 0.83))
        if X_PROVEN and rig.axis == 'Y':
            note('NOT   belts/pulleys/grubs - X homed on the SAME two')
            note('      motors and belts, so those are proven good')
            note('ALSO  home_current %sA +/-0.1; Y-only suspects are' % rig.current)
            note('      gantry racking, Y rails, gantry mass')
        else:
            note('ALSO  home_current %sA +/-0.1, belts, grubs' % rig.current)
        say('  RESULT: NOTHING WORKED - no %s value reached the rail.' % rig.field)
        say()
        say('  Do NOT keep sweeping the threshold. If every value fails the same way,')
        say('  the threshold is not the problem. Change ONE of these, then re-run:')
        say()
        say('   1. homing_speed   (now %s, rotation_distance is %s)'
              % (rig.speed, rig.rot_dist))
        if rig.speed <= rig.rot_dist:
            say('      *** THIS IS ALMOST CERTAINLY IT *** homing_speed MUST exceed')
            say('      rotation_distance for sensorless. Try %g.' % (rig.rot_dist * 1.5))
        else:
            say('      already above rotation_distance. Try %g for a stronger signal.'
                  % (rig.speed * 1.3))
        say()
        say('   2. coolstep_threshold   (now %s)' % rig.cool)
        say('      Keep it just BELOW homing_speed, about 0.8x, so StallGuard engages')
        say('      at steady speed rather than mid acceleration ramp. Try %g.'
              % (rig.speed * 0.83))
        say('      IF TRAVEL WAS IDENTICAL AT EVERY THRESHOLD, THIS IS THE CAUSE.')
        say()
        say('   3. home_current   (now %sA)' % rig.current)
        say('      Too LOW and the motors skip instead of stalling: the axis crawls,')
        say('      drifts diagonally on CoreXY, and StallGuard never sees a stall.')
        say('      Too HIGH and the frame takes a harder hit. Move by 0.1A.')
        say()
        if X_PROVEN and rig.axis == 'Y':
            say('   4. NOT the belts, pulleys or grub screws.')
            say('      X homed on this same pair of motors and this same pair of')
            say('      belts - on CoreXY an X move turns both of them - so the')
            say('      shared drivetrain is proven good. Do not go looking there.')
            say('      What Y does NOT share with X: the Y linear rails, and the')
            say('      whole gantry mass that only Y has to drag. If anything is')
            say('      mechanical it is gantry racking or a binding Y rail.')
        else:
            say('   4. mechanical. Loose belts or a loose pulley grub screw give exactly')
            say('      this signature - a slack belt absorbs the impact so the stall is')
            say('      soft and gradual, with nothing sharp to detect.')
    say('  ' + '-' * 68)
    return good


def verify(rig, sgt, runs, chain=True):
    rig.banner()
    rig.set_sgt(sgt)
    say('  verifying %s=%d over %d runs' % (rig.field, sgt, runs))
    say('  homing once first to reach the rail, then backing off %.0fmm before each run'
          % rig.expected)
    say()
    rig.one_home()
    res = []
    for i in range(1, runs + 1):
        rig.goto_start()
        ok, travel = rig.one_home()
        v = rig.classify(ok, travel)
        res.append(travel)
        run_spread = max(res) - min(res)
        say('  run %d/%d: travel %7.1fmm  [%-5s]  spread so far %.2fmm'
              % (i, runs, travel, v, run_spread))
        sys.stdout.flush()
    spread = max(res) - min(res)
    mean = sum(res) / len(res)
    say()
    say('  ' + '-' * 68)
    say('  spread %.2fmm over %d runs   (mean travel %.1fmm, expected %.0fmm)'
          % (spread, runs, mean, rig.expected))
    fails = [r for r in res if abs(r - rig.expected) > rig.expected * 0.1]
    rig.last_spread = spread
    rig.last_mean = mean
    note('-- repeatability --')
    note('%s=%d  %d runs  %.1fmm  spread %.2fmm'
         % (rig.field, sgt, runs, mean, spread))
    if fails:
        note('FAIL  %d/%d runs missed the rail' % (len(fails), runs))
        note('marginal - works only sometimes')
        note('DO    re-run FIND_%s_SGT, pick mid-window' % rig.axis)
    elif spread < 1.0:
        note('PASS  origin repeats exactly')
        changed, msg = apply_config(rig, sgt)
        note('CFG   ' + msg)
        if changed:
            note('DO    run FIRMWARE_RESTART to load it')
        if chain:
            note('')
            say()
            say('  passed - chaining to the home range test')
            distances(rig, [5, 15, 40, 120, 250], chain=chain, runs=runs)
            return
        note('NEXT  TEST_%s_HOME_RANGE' % rig.axis)
    elif spread < 3.0:
        note('MARGINAL  origin wanders %.2fmm' % spread)
        note('prints shift by that much')
        note('DO    check belt tension')
    else:
        note('FAIL  origin moves between homes')
        if X_PROVEN and rig.axis == 'Y':
            note('NOT   belts/grubs - shared with X, which passes')
            note('DO    check gantry racking + Y rails')
        else:
            note('DO    check belts + pulley grub screws')
    if fails:
        say('  FAIL - %d of %d runs did not reach the rail.' % (len(fails), runs))
        say('  This threshold is MARGINAL: it works sometimes. Do not use it.')
        say('  Re-run FIND_%s_SGT and pick a value nearer the MIDDLE of the window.'
              % rig.axis)
    elif spread < 1.0:
        say('  PASS - the origin lands in the same place every time. Use this value.')
        say()
        say('  MAKE IT PERMANENT ->  driver_%s: %d' % (rig.field.upper(), sgt))
        say('                        in [%s]' % rig.tmc_sec)
        say('  THEN:  TEST_%s_HOME_RANGE   then   MEASURE_%s_RAIL'
              % (rig.axis, rig.axis))
    elif spread < 3.0:
        say('  MARGINAL - usable, but the origin wanders by that much between homes')
        say('  and prints will shift by the same amount. Try a threshold one step')
        say('  further from the edge of the window, or check belt tension first.')
    else:
        say('  FAIL - the origin moves between homes. Not usable for printing.')
        if X_PROVEN and rig.axis == 'Y':
            say('  Not the belts or grub screws - X homes on the same motors and')
            say('  belts and repeats exactly. Look at gantry racking and the Y rails.')
        else:
            say('  Check belts and pulley grub screws BEFORE touching the threshold:')
            say('  a slack belt softens the stall and makes the trigger point wander.')
    short = rig.expected - mean
    if abs(short) > 3 and not fails:
        say()
        say('  NOTE: averaging %+.1fmm off the expected travel.' % (-short))
        say('  A consistent short stop means Klipper believes it is at position_endstop')
        say('  while the head is somewhere else - the WHOLE coordinate frame is offset')
        say('  by that much and every print shifts with it.')
        say('  Fix: %s %s by 1 so it reaches the rail, or set position_endstop to match.'
              % (rig.less_sensitive(), rig.field))
    say('  ' + '-' * 68)


def distances(rig, dists, chain=True, runs=10):
    """Home from a spread of starting distances. A home that only works from
    mid-travel is not much use: close to the rail StallGuard may still be loaded
    from the previous touch, and from the far end the axis builds momentum."""
    rig.banner()
    rig.set_sgt(rig.sgt_cfg)
    errs = []
    say('  homing from %s mm off the rail' % dists)
    say()
    rig.one_home()
    bad = []
    for d in dists:
        rig.ensure_frame()
        if not safe_abs(rig, rig.pos_max - d, 'placing the head %.0fmm out' % d):
            say('    cannot place the head for the %.0fmm test - skipping it' % d)
            continue
        rig.at_rail = True
        p0 = axis_pos(rig.axis)
        a0, b0 = mcu_steps()
        try:
            post('G28 ' + rig.axis)
            ok = True
        except urllib.error.HTTPError:
            ok = False
        time.sleep(0.6)
        a1, b1 = mcu_steps()
        da, db = a1 - a0, b1 - b0
        rig.at_rail = ok
        raw = (da + db) / 2.0 if rig.axis == 'X' else (da - db) / 2.0
        travel = raw * rig.step_dist + rig.backoff
        ##  Anchor from the rail contact instead of inferring the position.
        ##  correct_frame guesses from p0 - the position BEFORE the home - and a
        ##  wrong p0 makes the correction wrong, which shifts the next absolute
        ##  move, which corrupts the next measurement. That compounded at ~2.8mm
        ##  per run here and read as a distance-dependent error, which is
        ##  exactly what a real mechanical fault looks like. After a successful
        ##  home the position is known, so say so.
        if ok:
            post('SET_KINEMATIC_POSITION %s=%.3f'
                 % (rig.axis, rig.pos_max - rig.backoff))
            time.sleep(0.3)
        else:
            correct_frame(rig, p0, raw)
        err = travel - d
        verdict = 'OK' if (ok and abs(err) < 3.0) else (
            'NO TRIGGER' if not ok else 'OFF by %+.1fmm' % err)
        if verdict != 'OK':
            bad.append(d)
        errs.append(err)
        say('  start %6.1fmm off rail -> travel %7.1fmm (expected %6.1f)   %s'
            % (d, travel, d, verdict))
    say()
    note('-- home range --')
    note('from %s mm off the rail' % dists)
    # A CONSTANT error at every distance cannot be a homing fault. Homing from
    # 5mm and from 250mm share nothing except the starting reference, so if both
    # are wrong by the same amount it is the reference that is wrong - the axis
    # was declared at a position it was not physically at. Reporting that as a
    # failure and sending the user to raise unload_dist is worse than useless:
    # it blames a setting that is not involved and hides the real cause.
    # Judge uniformity only on distances BEYOND unload_dist. At or below it the
    # macro adds a pre-home backoff, so those points carry an extra offset by
    # design and would mask an otherwise clean constant error.
    try:
        _v = query('gcode_macro _SENSORLESS_VARS')['gcode_macro _SENSORLESS_VARS']
        _unload = float(_v.get('unload_dist', 0) or 0)
    except Exception:
        _unload = 0.0
    clean = [e for d, e in zip(dists, errs) if d > _unload]
    if len(clean) < 3:
        clean = errs
    err_spread = (max(clean) - min(clean)) if clean else 0.0
    err_mean = (sum(clean) / len(clean)) if clean else 0.0
    uniform = len(clean) >= 3 and err_spread < 4.0 and abs(err_mean) > 3.0
    rig.last_errs = list(errs)
    rig.last_bad = list(bad)
    if bad and uniform:
        note('OFFSET  every distance out by ~%+.1fmm - NOT a homing fault'
             % err_mean)
        note('the axis homed correctly each time. The STARTING')
        note('reference was wrong by that much.')
        if rig.axis == 'Y':
            note('DO    re-centre the head by hand, then run again')
            note('      Y is declared, never measured - a placement')
            note('      %.0fmm out shifts every result by %.0fmm'
                 % (abs(err_mean), abs(err_mean)))
        else:
            note('DO    check position_max, and that nothing moved')
            note('      the axis between homing and the test')
        say('  Every distance is out by about %+.1fmm - a CONSTANT error.' % err_mean)
        say('  Homing from 5mm and from 250mm have nothing in common except the')
        say('  starting reference, so that is what is wrong, not the axis. The')
        say('  homes themselves were fine.')
    elif bad:
        note('FAIL  at %s' % bad)
        note('errors %s vary, so this is the axis'
             % [round(e, 1) for e in errs])
        note('DO    raise unload_dist in _SENSORLESS_VARS')
        say('  FAILED from these distances: %s' % bad)
        say('  Close to the rail usually means StallGuard is still loaded from the')
        say('  previous touch. Raise _SENSORLESS_VARS unload_dist so it backs off')
        say('  further before homing.')
    else:
        note('PASS  homes from every distance')
        if chain:
            note('')
            say()
            say('  passed - chaining to the rail measurement')
            # Carry the user's RUNS through instead of a hardcoded 10, so one
            # parameter governs the whole chain rather than only its first test.
            measure_axis(rig, runs)
            return
        say('  PASS - homes correctly from every distance tested.')


def measure_axis(rig, runs=10):
    """Measure the TRUE usable length of the axis, rail to rail.

    Home at the max end, drive to position_min, home again, and read the real
    distance from MCU step counts. If measured < configured, the head is hitting
    the far rail BEFORE position_min - the config claims travel the machine does
    not have, and every print is squeezed into a frame that does not exist."""
    rig.banner()
    rig.set_sgt(rig.sgt_cfg)
    pmin = query('configfile')['configfile']['settings'][
        'stepper_' + rig.axis.lower()]['position_min']
    claimed = rig.pos_max - pmin
    say('  config claims %.1fmm of travel (position_min %.1f, position_max %.1f)'
        % (claimed, pmin, rig.pos_max))
    say('  measuring the real rail-to-rail distance over %d runs' % runs)
    say()
    ##  Two establishing homes, not one. The first anchors the frame to a real
    ##  rail contact; the second guarantees the move to position_min that
    ##  follows starts from a position that is actually true. With only one, the
    ##  first measurement inherits whatever frame the previous test left behind
    ##  and reads ~135mm short while every later run is exact.
    rig.one_home()
    ##  We have just touched the rail, so the position is KNOWN - not inferred.
    ##  State it outright rather than letting correct_frame guess from a p0 that
    ##  may itself be inherited wrong. Without this the first measurement reads
    ##  ~135mm short while every later one is exact, because each subsequent
    ##  home re-anchors the frame the hard way.
    post('SET_KINEMATIC_POSITION %s=%.3f' % (rig.axis, rig.pos_max - rig.backoff))
    time.sleep(0.4)
    ##  Anchored to a real rail contact, so the frame is trustworthy again.
    rig.frame_ok = True
    res = []
    for i in range(1, runs + 1):
        rig.ensure_frame()
        if not safe_abs(rig, pmin, 'moving to the far end to measure the rail'):
            say('    frame unverified - stopping the rail measurement here')
            break
        rig.at_rail = True
        p0 = axis_pos(rig.axis)
        a0, b0 = mcu_steps()
        try:
            post('G28 ' + rig.axis)
            ok = True
        except urllib.error.HTTPError:
            ok = False
        time.sleep(0.6)
        a1, b1 = mcu_steps()
        da, db = a1 - a0, b1 - b0
        rig.at_rail = ok
        raw = (da + db) / 2.0 if rig.axis == 'X' else (da - db) / 2.0
        travel = raw * rig.step_dist + rig.backoff
        ##  NO correct_frame here. Every iteration ends on a real rail contact,
        ##  so the home itself anchors the frame - there is nothing to correct.
        ##  Calling it anyway was the bug: it compared this run's travel against
        ##  the expected full-length travel, judged the home false, and rewrote
        ##  the position to a value ~135mm short. The next G1 to position_min
        ##  then started from that fiction, so the head never reached the front,
        ##  the following home travelled less, and the error compounded - a
        ##  clean +2mm per run that looked like a physical measurement.
        res.append(travel)
        say('  run %d/%d: rail-to-rail %7.2fmm   %s'
            % (i, runs, travel, 'measured' if ok else 'NO TRIGGER'))
    say()
    mean = sum(res) / len(res)
    say('  measured %.2fmm  (spread %.2fmm)   config claims %.2fmm'
        % (mean, max(res) - min(res), claimed))
    diff = mean - claimed
    note('-- rail measure --')
    note('measured %.2fmm  spread %.2fmm' % (mean, max(res) - min(res)))
    note('config   %.2fmm claimed' % claimed)
    if abs(diff) < 1.0:
        note('MATCH  position_max is correct')
        say('  MATCH - position_max is correct.')
    elif diff < 0:
        note('SHORT by %.1fmm' % -diff)
        note('DO    cut position_max by ~%.0fmm' % -diff)
        say('  SHORT by %.1fmm. The head reaches the far rail BEFORE position_min,' % -diff)
        say('  so the config claims travel the machine does not have.')
        say('  FIX: reduce position_max by about %.0fmm, or raise position_min.' % -diff)
    else:
        say('  LONGER by %.1fmm than configured - there is unused travel available.' % diff)
        say('  You could raise position_max by up to %.0fmm.' % diff)


def axis_pos(axis):
    """Where Klipper currently believes the axis is."""
    return query('toolhead')['toolhead']['position'][0 if axis == 'X' else 1]


def correct_frame(rig, p0, raw):
    """Re-align Klipper's frame with reality, but ONLY after a FALSE trigger.

    Klipper sets the axis to position_max on any successful home. When the home
    genuinely reached the rail that value is exact and authoritative - the rail
    is a hard physical reference, better than anything derived from step counts,
    which carry the error of p0 with them. Overwriting a good home with an
    estimate is how the position ends up wrong AFTER a home that worked.

    When the trigger was false the head is nowhere near position_max, and every
    later absolute move is computed from that fiction - which is what drove the
    head into the opposite rail. There the step counters are the only truth
    available, so use them.

    So: decide which happened by comparing the distance actually covered against
    the distance to the rail from where we started.
    """
    moved = raw * rig.step_dist              # net displacement, backoff included
    reached = moved + rig.backoff            # distance covered before it tripped
    to_rail = rig.pos_max - p0               # distance it SHOULD have covered
    tol = max(3.0, abs(to_rail) * 0.05)
    if to_rail > 0 and abs(reached - to_rail) <= tol:
        # Genuine home. Klipper is right; leave it alone.
        return None
    true_now = max(rig.pos_min, min(rig.pos_max, p0 + moved))
    believed = axis_pos(rig.axis)
    if abs(believed - true_now) <= 1.0:
        return true_now
    post('SET_KINEMATIC_POSITION %s=%.3f' % (rig.axis, true_now))
    time.sleep(0.25)
    say('    false trigger: klipper said %s=%.1f, really %.1f (out by %.1fmm)'
        % (rig.axis, believed, true_now, believed - true_now))
    return true_now


def set_home_current(amps, axis='X'):
    """Change the homing current the sensorless macro uses.

    Y writes to its OWN variable: the axes share one home_current by default,
    but X's threshold is calibrated at that value, so a Y experiment written
    into the shared variable would silently re-tune X.
    """
    var = 'y_home_current' if axis == 'Y' else 'home_current'
    post('SET_GCODE_VARIABLE MACRO=_SENSORLESS_VARS VARIABLE=%s VALUE=%.3f'
         % (var, amps))
    time.sleep(0.3)
    say('  homing current for %s set to %.2fA  (%s)' % (axis, amps, var))


def edit_cfg(head, key, value):
    """Set key inside [head] of printer.cfg.

    Section-scoped deliberately: X and Y carry identical key names, so an
    unscoped replace rewrites the other axis - the tuned one - in silence.
    """
    import re as _re
    text = open(CONFIG_FILE).read()
    marker = '[' + head + ']'
    if marker not in text:
        return False
    start = text.index(marker)
    nxt = text.find('\n[', start + 1)
    end = nxt if nxt != -1 else len(text)
    chunk = text[start:end]
    pat = '^' + _re.escape(key) + r':\s*\S+'
    if not _re.search(pat, chunk, flags=_re.M):
        return False
    chunk = _re.sub(pat, '%s: %s' % (key, value), chunk, count=1, flags=_re.M)
    open(CONFIG_FILE, 'w').write(text[:start] + chunk + text[end:])
    return True


def park_centre(rig):
    """Return the axis to mid-travel between combinations.

    Two cases, and the second is the one that used to break:

    - The axis is homed. Klipper knows where it is, so an absolute move works.
    - The axis is NOT homed, because every threshold failed and the last attempt
      ground into the rail. G28 errored, so Klipper cleared the homed state and
      rejects any move with "Must home axis first". The old code just threw here,
      and the next combination then declared the axis at CENTRE while the head
      was physically against the rail - so its first home started from the wrong
      end and drove straight back into the stop.

    A grind is not a lost position though: pushing against the rail is exactly
    where position_max is. So declare that, then move off it normally. An early
    trigger cannot reach this path, because there G28 succeeded and the axis is
    homed.
    """
    mid = (rig.pos_min + rig.pos_max) / 2.0
    try:
        homed = rig.axis.lower() in query('toolhead')['toolhead']['homed_axes']
    except Exception:
        homed = False
    try:
        if not homed:
            ##  This used to DECLARE the axis at position_max, reasoning that it
            ##  "ended against the rail", and then move on that. It is a guess,
            ##  and parking is a convenience - never worth a guess that can send
            ##  the head the length of the axis.
            say('  %s is unhomed and its position is unknown - not parking.'
                % rig.axis)
            say('  The head stays where it is; the next home will place it.')
            return
        rig.frame_ok = True
        safe_abs(rig, mid, 'parking at centre')
        return True
    except urllib.error.HTTPError:
        say('  could not park %s at centre - hand-centre it before the next run'
            % rig.axis)
        return False


def coarse_to_fine(rig, lo, hi):
    """Sweep the WHOLE threshold range at single-integer resolution.

    This used to walk coarsely first and refine afterwards, to save homing at
    128 values. That optimisation was wrong: a working window can be ONE integer
    wide, and a step-4 pass steps straight over it. On this machine it tested 0
    (stopped short) and 4 (ground the rail), never touched 1..3, and reported
    "no threshold works" at settings that had been proven working by hand
    minutes earlier. Every conclusion drawn from those runs was unsound.

    Step 1 cannot skip a window. It is slower, but the sweep still stops at the
    first grind, so the cost is bounded by where the window actually is rather
    than by the width of the range.
    """
    step = 1 if hi >= lo else -1
    return sweep(rig, lo, hi, step, chain=False) or []


def matrix(axis, accels, lo, hi):
    """Sweep threshold x accel, using RUNTIME changes only.

    This deliberately does NOT touch homing_speed. homing_speed is a config
    option with no runtime command, so sweeping it meant rewriting printer.cfg
    and forcing a firmware restart - and on a USB-to-CAN bridge each restart
    resets the host MCU, drops its USB device and destroys can0, briefly taking
    the CAN toolhead board with it. A StallGuard tuning tool has no business
    managing firmware restarts, USB re-enumeration or MCU liveness, and every
    abort so far came from that machinery rather than from the tuning.

    So speed is a fixed input here: whatever printer.cfg currently says. To try
    a different one, change homing_speed deliberately and run this again. The
    report states the speed it ran at so results are never ambiguous.
    """
    results = []
    rig0 = Rig(axis)
    speed = rig0.speed
    ##  The homing current is REPORTED here, never swept. It used to be iterated
    ##  and printed in every combination header while never actually being
    ##  applied - so a report showed three currents for three runs that all ran
    ##  at the same one, and any difference between them read as a current
    ##  effect when it was only run-to-run variation. That is how this tool
    ##  produced "1.0A works, 0.8A and 1.2A do not" from three identical runs.
    ##  Sweeping it is also the one change an aborted run cannot safely undo,
    ##  which strands the drivers at the homing current for every later home.
    ##  Change it deliberately with SET_HOME_CURRENT and re-run: a threshold is
    ##  only valid at the current it was tuned at.
    total = len(accels)
    n = 0
    say('  homing_speed is %g (from printer.cfg) and is NOT swept here.' % speed)
    say('  To test another speed, change it, restart, and re-run.')
    say('  homing current is %.2fA and is NOT swept either - see SET_HOME_CURRENT.'
        % rig0.current)
    say('  %d combinations: accels %s' % (total, accels))
    say('  threshold range %d..%d, full span, coarse then fine' % (lo, hi))
    say()
    for ac in accels:
        ##  Single iteration at the current actually in force. Left as a loop so
        ##  the body below is untouched; see the note above on why it is not swept.
        for cu in [rig0.current]:
            n += 1
            post('SET_GCODE_VARIABLE MACRO=_SENSORLESS_VARS '
                 'VARIABLE=%s_home_accel VALUE=%d' % (axis.lower(), ac))
            time.sleep(0.3)
            rig = Rig(axis)
            say()
            say('  === %d/%d  accel %d  current %.2fA  (speed %g) ==='
                % (n, total, ac, cu, speed))
            if axis == 'Y':
                okx, why = require_x_calibrated()
                if not okx:
                    say('  ' + why)
                    return results
            mark = len(SUMMARY)
            try:
                good = coarse_to_fine(rig, lo, hi)
            except Exception as exc:
                say('  combination failed: %s' % exc)
                good = []
            del SUMMARY[mark:]
            results.append({'speed': speed, 'accel': ac, 'current': cu,
                            'good': good, 'width': len(good)})
            say('  -> window %s (%d wide)'
                % (good if good else 'none', len(good)))
            park_centre(rig)

    # Window width alone is not enough: a combination can reach the rail at
    # several thresholds and still stop somewhere different each time, which is
    # what prints as a layer shift. The finalists are re-tested for
    # REPEATABILITY, and that decides the winner.
    finalists = sorted([r for r in results if r['width'] > 0],
                       key=lambda r: -r['width'])[:4]
    if finalists:
        say()
        say('  === repeatability check on the %d best ===' % len(finalists))
        for r in finalists:
            post('SET_GCODE_VARIABLE MACRO=_SENSORLESS_VARS '
                 'VARIABLE=%s_home_accel VALUE=%d' % (axis.lower(), r['accel']))
            rig = Rig(axis)
            if axis == 'Y':
                okx, _why = require_x_calibrated()
                if not okx:
                    break
            pick = r['good'][len(r['good']) // 2]
            mark = len(SUMMARY)
            try:
                verify(rig, pick, 5, chain=False)
                r['spread'] = getattr(rig, 'last_spread', None)
            except Exception as exc:
                say('  repeatability failed: %s' % exc)
                r['spread'] = None
            del SUMMARY[mark:]
            r['sgt'] = pick
            say('  ac%d %.2fA sgt%d -> spread %s'
                % (r['accel'], r['current'], pick,
                   ('%.2fmm' % r['spread']) if r['spread'] is not None else 'n/a'))
            park_centre(rig)
    return results


def failure_advice(axis, rig, why):
    """Say exactly what to change when no threshold works.

    None of these are swept automatically any more. homing_speed is config-only,
    and accel and current are deliberately left alone so a sweep cannot leave the
    machine in a state it did not start in - an aborted run used to strand the
    drivers at the homing current, and every later home re-confirmed it.

    So the tool measures and advises; the changes are yours to make. One at a
    time, re-testing between, because each of these invalidates the threshold
    tuned against the others.
    """
    a = axis.lower()
    try:
        v = query('gcode_macro _SENSORLESS_VARS')['gcode_macro _SENSORLESS_VARS']
        accel = float(v.get('%s_home_accel' % a, 0) or 0)
    except Exception:
        accel = 0
    note('')
    note(why)
    note('change ONE of these, then re-run:')
    note('')

    note('1. homing_speed - now %g' % rig.speed)
    cands = []
    for mult in (1.3, 1.6, 0.75):
        c = round(rig.speed * mult / 5.0) * 5
        if c > rig.rot_dist and c != rig.speed and c not in cands:
            cands.append(c)
    for c in cands:
        note('     %-4g with coolstep_threshold %g' % (c, round(c * 0.83)))
    note('   StallGuard reads load from back-EMF, so too slow leaves it')
    note('   nothing to read and no threshold can rescue that. Must stay')
    note('   above rotation_distance %g.' % rig.rot_dist)
    note('   [stepper_%s] homing_speed' % a)
    note('   [tmc5160 stepper_%s] coolstep_threshold' % a)
    note('   keep them paired - coolstep just under the speed, or')
    note('   StallGuard ends up reading the acceleration ramp.')
    note('   needs FIRMWARE_RESTART.')
    note('')

    note('2. homing accel - now %d' % accel if accel else '2. homing accel - not set')
    if accel:
        note('     try %d or %d' % (int(accel * 0.5), int(accel * 1.5)))
    else:
        note('     try 1000, or 500')
    note('   Acceleration load can sit close to the stall load on a heavy')
    note('   axis, leaving no gap for a threshold to live in. Too low and')
    note('   momentum carries the axis past the trigger point.')
    note('   _SENSORLESS_VARS  variable_%s_home_accel' % a)
    note('')

    note('3. home_current - now %.2fA' % rig.current)
    note('     try %.1fA or %.1fA, in 0.1A steps'
         % (max(0.4, rig.current - 0.2), rig.current + 0.2))
    note('   Too low and the motor skips instead of stalling - a skid is')
    note('   soft and gradual, and StallGuard cannot pick it out from')
    note('   acceleration. Too high and the stall is blunt, and the frame')
    note('   takes the hit. 0 = home at the configured run current.')
    note('   _SENSORLESS_VARS  variable_home_current')
    note('')

    note('4. mechanical - check LAST, and only if the other axis fails')
    note('   too. On CoreXY both motors and both belts turn for either')
    note('   axis, so an axis that passes proves the shared drivetrain:')
    note('   belts, pulleys and grub screws are all exonerated by it.')
    note('   What is NOT shared: that axis own linear rails, and on Y the')
    note('   whole gantry mass it alone has to drag.')


def safe_abs(rig, coord, why):
    """Absolute move, but only on a frame something actually verified.

    Every crash this tool has caused was an absolute move computed from a
    coordinate nothing had checked. Guarding goto_start() fixed one instance and
    left the identical pattern in three other functions, so it is a helper now
    and there is exactly one place to get it right.

    Refusing is always safe: the head stays put and the next homing move
    re-establishes the frame honestly. Moving on a bad frame is not.
    """
    if not getattr(rig, 'frame_ok', False):
        say('    (skipping %s - frame unverified, staying put)' % why)
        return False
    post('G90')
    post('G1 %s%.1f F6000' % (rig.axis, coord))
    post('M400')
    time.sleep(0.5)
    return True


def ask_operator(title, lines, timeout=1800.0):
    """Drop the motors, put a dialog on the operator's screen, and WAIT.

    Returns True to continue, False to cancel.

    This exists because the tool cannot see what it did to the machine. A grind
    racks the gantry, and whether it came back square is a question only a human
    standing at the printer can answer. Guessing produced a 197mm "spread" once
    and would have thrown away a good threshold on the strength of it.

    Motors are released first so the gantry can be squared by hand, which is the
    whole point of stopping here.
    """
    ##  The gate macros live in sensorless_tools.cfg. Anyone who updates this
    ##  script without updating their config would otherwise hit an exception
    ##  HERE - mid-sweep, straight after a grind - which is the worst possible
    ##  moment for the safety feature itself to be the thing that fails.
    ##
    ##  Missing macros must therefore fail SAFE: stop, do not measure, and say
    ##  exactly what to install. Never silently carry on past a gate.
    try:
        post('SET_GCODE_VARIABLE MACRO=_SGT_GATE VARIABLE=go VALUE=0')
    except Exception:
        post('M84')
        say()
        say('  ' + '=' * 60)
        say('  STOPPING - cannot ask you, so I will not guess.')
        say('  This needs the _SGT_GATE / SGT_CONTINUE / SGT_ABORT macros from')
        say('  sensorless_tools.cfg. Your config predates them, so update it')
        say('  and RESTART. Motors are off; the result so far is kept.')
        say('  ' + '=' * 60)
        return False
    post('M84')
    post('RESPOND TYPE=command MSG="action:prompt_begin %s"' % title)
    for ln in lines:
        post('RESPOND TYPE=command MSG="action:prompt_text %s"' % ln)
    post('RESPOND TYPE=command MSG='
         '"action:prompt_footer_button Continue|SGT_CONTINUE|primary"')
    post('RESPOND TYPE=command MSG='
         '"action:prompt_footer_button Cancel|SGT_ABORT|error"')
    post('RESPOND TYPE=command MSG="action:prompt_show"')
    say()
    say('  ' + '=' * 60)
    say('  WAITING FOR YOU - %s' % title)
    for ln in lines:
        say('  %s' % ln)
    say('  Motors are OFF. Square the gantry by hand, then answer the popup,')
    say('  or run  SGT_CONTINUE  /  SGT_ABORT  from the console.')
    say('  ' + '=' * 60)
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(2.0)
        try:
            g = query('gcode_macro _SGT_GATE')['gcode_macro _SGT_GATE']
            go = int(float(g.get('go', 0) or 0))
        except Exception:
            ##  A restart or a Klipper error while we wait. Keep waiting rather
            ##  than treating a transient query failure as consent.
            continue
        if go == 1:
            say('  -> continuing')
            return True
        if go == 2:
            say('  -> cancelled by operator')
            return False
    post('RESPOND TYPE=command MSG="action:prompt_end"')
    say('  -> no answer in %.0f minutes, cancelling' % (timeout / 60.0))
    return False


def set_var(name, value):
    post('SET_GCODE_VARIABLE MACRO=_SENSORLESS_VARS VARIABLE=%s VALUE=%s'
         % (name, value))
    time.sleep(0.3)


def compare_homing(axis, dists, second_mm, unload_mm):
    """Run the home-range test under each runway strategy and show them together.

    The claim being tested: a blind pre-home backoff (unload_dist) and a second
    homing move (second_home_dist) both exist to give StallGuard runway, but only
    the homing move is monitored and can stop on contact. This measures whether
    either actually fixes the close-in distances, rather than asking anyone to
    take it on trust.

    Restores whatever the variables were on the way out, including after a
    failure, so a comparison run cannot leave the machine configured oddly.
    """
    v = query('gcode_macro _SENSORLESS_VARS')['gcode_macro _SENSORLESS_VARS']
    was_second = float(v.get('second_home_dist', 0) or 0)
    was_unload = float(v.get('unload_dist', 0) or 0)

    combos = [
        ('neither      ', 0, 0),
        ('unload only  ', 0, unload_mm),
        ('2nd home only', second_mm, 0),
        ('both         ', second_mm, unload_mm),
    ]
    results = []
    try:
        for label, sec, unl in combos:
            set_var('second_home_dist', sec)
            set_var('unload_dist', unl)
            rig = Rig(axis)
            say()
            say('  === %s   second_home=%g  unload=%g ===' % (label.strip(), sec, unl))
            if axis == 'Y':
                okx, why = require_x_calibrated()
                if not okx:
                    say('  ' + why)
                    break
            mark = len(SUMMARY)
            try:
                distances(rig, dists, chain=False)
                errs = getattr(rig, 'last_errs', [])
                bad = getattr(rig, 'last_bad', [])
            except Exception as exc:
                say('  combination failed: %s' % exc)
                errs, bad = [], list(dists)
            del SUMMARY[mark:]
            worst = max([abs(e) for e in errs]) if errs else float('nan')
            results.append((label, sec, unl, errs, bad, worst))
            say('  -> worst error %.1fmm, failed at %s'
                % (worst, bad if bad else 'nothing'))
            park_centre(rig)
    finally:
        set_var('second_home_dist', was_second)
        set_var('unload_dist', was_unload)
        say()
        say('  restored second_home_dist=%g unload_dist=%g' % (was_second, was_unload))

    note('-- homing strategy comparison --')
    note('distances %s' % dists)
    note('')
    for label, sec, unl, errs, bad, worst in results:
        note('%s worst %5.1fmm  fails %s'
             % (label, worst, len(bad)))
    note('')
    clean = [r for r in results if not r[4]]
    if not clean:
        note('FAIL  no strategy homed correctly from every distance')
        note('DO    the runway is not the problem - see the sweep advice')
    else:
        # Prefer the safest that works: a homing move stops on contact, a blind
        # G1 does not, so if both pass, the second home is the better answer.
        order = {'2nd home only': 0, 'both         ': 1, 'unload only  ': 2,
                 'neither      ': 3}
        clean.sort(key=lambda r: (order.get(r[0], 9), r[5]))
        best = clean[0]
        note('BEST  %s (worst %.1fmm)' % (best[0].strip(), best[5]))
        if best[0].strip() == 'neither':
            note('this axis needs no runway help at all')
        elif 'unload' in best[0] and '2nd' not in best[0]:
            note('WARN  unload works here but is a BLIND move - it cannot')
            note('      stop on contact and drives on a possibly wrong frame')


def report_matrix(axis, results):
    ok = [r for r in results if r['width'] > 0]
    note('-- parameter matrix --')
    note('%d combinations at homing_speed %g'
         % (len(results), results[0]['speed'] if results else 0))
    if not ok:
        note('FAIL  no combination produced a window')
        failure_advice(axis, Rig(axis), 'nothing reached the rail at any threshold.')
        return
    # Measured repeatability beats window width: a wide window that wanders is
    # useless, a narrow one that repeats is printable. Unmeasured combinations
    # sort last rather than being treated as perfect.
    def rank(r):
        sp = r.get('spread')
        return (0 if sp is not None else 1,
                round(sp, 2) if sp is not None else 999,
                -r['width'])

    ok.sort(key=rank)
    note('ranked by spread, then window width:')
    for r in ok[:6]:
        sp = r.get('spread')
        note('sp%-4g ac%-5d %.2fA win%-2d %s'
             % (r['speed'], r['accel'], r['current'], r['width'],
                ('spread %.2fmm' % sp) if sp is not None else 'not measured'))
    b = ok[0]
    pick = b.get('sgt') or b['good'][len(b['good']) // 2]
    sp = b.get('spread')
    note('')
    note('BEST  speed %g  accel %d  current %.2fA'
         % (b['speed'], b['accel'], b['current']))
    note('      sgt %d, window %d wide' % (pick, b['width']))
    if sp is None:
        note('WARN  repeatability was never measured for this one')
    elif sp < 0.5:
        note('PASS  spread %.2fmm - printable' % sp)
    elif sp < 1.5:
        note('MARGINAL  spread %.2fmm - prints shift by that much' % sp)
    else:
        note('FAIL  spread %.2fmm - not usable, keep looking' % sp)
    if b['width'] == 1:
        note('WARN  window is 1 value wide - fragile when warm')
    # A result that reaches the rail but wanders, or one balanced on a single
    # integer, is not finished - and speed is the dial this cannot turn itself.
    if b['width'] == 1 or (sp is not None and sp >= 0.5):
        failure_advice(axis, Rig(axis),
                       'this works but has no margin to spare.')


def require_x_calibrated():
    """Y may not be tested until X homes for real. Returns (ok, reason).

    Y drags the whole gantry and its every move is resolved through the X
    coordinate frame. If X has never been homed, that frame is a guess, and a
    Y test then measures the guess rather than the axis - which is how a sweep
    reports a 300mm 'travel' that is really a crash into the rail.

    The check is the honest one: actually home X. Nothing else proves X is
    tuned, and if X cannot home then no Y number would have meant anything.
    """
    say('  Y needs a real X frame first - homing X')
    try:
        post('G28 X')
    except urllib.error.HTTPError:
        return False, 'X will not home - tune X first (FIND_X_SGT, VERIFY_X_HOME)'
    time.sleep(0.7)
    if 'x' not in query('toolhead')['toolhead']['homed_axes']:
        return False, 'X did not home - tune X first (FIND_X_SGT, VERIFY_X_HOME)'
    global X_PROVEN
    X_PROVEN = True
    say('  X homed - frame is real')

    # Centre X, but NEVER move Y.
    #
    # Y's physical position is genuinely unknown before the first home: nothing
    # has measured it. An earlier version declared Y at the rail and drove it
    # forward to centre, which is only correct if that guess is right - if the
    # head was already forward, that move drives it into the FRONT rail. There
    # is no safe way to move an axis whose position you do not know.
    #
    # So the head is placed at Y centre BY HAND before the sweep, and the only
    # thing done here is to tell Klipper where it already is. X is different:
    # it was just homed for real, so moving it is safe and exact.
    cfg = query('configfile')['configfile']['settings']
    stx, sty = cfg['stepper_x'], cfg['stepper_y']
    midx = (stx['position_min'] + stx['position_max']) / 2.0
    midy = (sty['position_min'] + sty['position_max']) / 2.0
    post('G90')
    post('G1 X%.1f F6000' % midx)
    post('M400')
    time.sleep(0.5)
    say('  X centred at %.0f. Y is NOT moved - its position is unknown.' % midx)
    say('  ASSUMING the head was placed at Y centre by hand, as instructed.')
    say('  declaring Y=%.0f without moving. If the head is not near centre,'
        % midy)
    say('  STOP NOW: the first home would travel from the wrong place.')
    post('SET_KINEMATIC_POSITION Y=%.0f' % midy)
    time.sleep(0.3)
    say()
    say()
    return True, ''


def main():
    args = sys.argv[1:]
    # By default a passing test flows into the next one:
    #   sweep -> repeatability -> home range -> rail measure
    # A failure stops the chain there, since later tests would be meaningless.
    chain = True
    if '--nochain' in args:
        chain = False
        args.remove('--nochain')
    if '--current' in args:
        i = args.index('--current')
        # The axis token can sit after the flag, so find it rather than assuming
        # args[0] - otherwise a Y run would write X's shared current.
        say('  --current is no longer supported: homing uses the current in'
            ' _SENSORLESS_VARS, so a run cannot strand the drivers elsewhere')
        del args[i:i + 2]
    # Every Y path is gated on X, before any Y motion is commanded.
    if args and args[0].upper() == 'Y':
        ok, why = require_x_calibrated()
        if not ok:
            note('REFUSED  ' + why)
            note('Y moves resolve through the X frame. Without a homed X')
            note('a Y sweep measures a guess, and grinds into the rail.')
            post('M84')
            flush_summary('SUMMARY - Y REFUSED (X NOT CALIBRATED)')
            return
    if '--measure' in args:
        i = args.index('--measure')
        axis = args[0] if i > 0 else 'X'
        runs = int(args[i + 1]) if len(args) > i + 1 else 10
        measure_axis(Rig(axis), runs)
        title = 'SUMMARY - %s RAIL MEASURE' % axis.upper()
    elif '--compare' in args:
        i = args.index('--compare')
        axis = next((a.upper() for a in args if a.upper() in ('X', 'Y')), 'X')
        nums = []
        for tok in args[i + 1:]:
            try:
                nums.append(float(tok))
            except ValueError:
                break
        second_mm = nums[0] if len(nums) > 0 else 40.0
        unload_mm = nums[1] if len(nums) > 1 else 40.0
        ds = [5.0, 15.0, 40.0, 120.0, 250.0]
        say('  comparing runway strategies on %s' % axis)
        say('  second_home_dist=%g  unload_dist=%g  distances %s'
            % (second_mm, unload_mm, ds))
        say('  4 combinations x %d homes - this takes a while.' % len(ds))
        say()
        compare_homing(axis, ds, second_mm, unload_mm)
        title = 'SUMMARY - %s HOMING STRATEGY' % axis
    elif '--matrix' in args:
        i = args.index('--matrix')
        axis = next((a.upper() for a in args if a.upper() in ('X', 'Y')), 'Y')

        def _lst(flag, default):
            if flag not in args:
                return default
            j = args.index(flag)
            out = []
            for tok in args[j + 1:]:
                try:
                    out.append(float(tok))
                except ValueError:
                    break
            return out or default

        # 1000 is in the default list because it is the only accel that has
        # actually produced a working Y window. 500 collapsed it entirely.
        accels = [int(a) for a in _lst('--accels', [1000.0, 1500.0])]
        if _lst('--currents', []):
            say('  --currents is IGNORED: the homing current is not swept.')
            say('  An aborted sweep cannot reliably put it back, so set it')
            say('  with SET_HOME_CURRENT and re-run.')
        rig0 = Rig(axis)
        # Default: the driver's entire range, most sensitive end first.
        lo, hi = int(rig0.sg_start), int(rig0.sg_end)
        req = _lst('--sgt', [])
        if len(req) >= 2:
            # A user range is honoured but CLAMPED to what the driver actually
            # accepts. Writing a value outside the register's range does not
            # fail loudly - it wraps or saturates - so the sweep would report
            # results for a threshold the driver never held.
            d_lo, d_hi = min(rig0.sg_lo, rig0.sg_hi), max(rig0.sg_lo, rig0.sg_hi)
            want_lo, want_hi = int(req[0]), int(req[1])
            lo = max(d_lo, min(d_hi, want_lo))
            hi = max(d_lo, min(d_hi, want_hi))
            if (lo, hi) != (want_lo, want_hi):
                say('  requested %d..%d is outside what %s accepts (%d..%d)'
                    % (want_lo, want_hi, rig0.drv, d_lo, d_hi))
                say('  clamped to %d..%d' % (lo, hi))
            else:
                say('  sweeping %d..%d as requested (%s allows %d..%d)'
                    % (lo, hi, rig0.drv, d_lo, d_hi))
        else:
            say('  sweeping the full %s range %d..%d, most sensitive first'
                % (rig0.drv, lo, hi))
        combos = len(accels)
        per = abs(hi - lo) + 1
        say('  ESTIMATE: %d combinations x ~%d homes = ~%d homes, roughly %d min'
            % (combos, per, combos * per, combos * per * 8 // 60))
        say('  Narrow it with ACCELS= or SGT_FROM=/SGT_TO= if too long.')
        say('  STOP_SGT_DIAG aborts at any point.')
        say()
        res = matrix(axis, accels, lo, hi)
        report_matrix(axis, res)
        title = 'SUMMARY - %s PARAMETER MATRIX' % axis
    elif '--range' in args:
        i = args.index('--range')
        axis = args[0] if i > 0 else 'X'
        ds = [float(x) for x in args[i + 1:]] or [5, 20, 50, 150, 250]
        runs_opt = 10
        distances(Rig(axis), ds, chain=chain, runs=runs_opt)
        title = 'SUMMARY - %s HOME RANGE' % axis.upper()
    elif '--verify' in args:
        i = args.index('--verify')
        axis = args[0] if i > 0 else 'X'
        rig = Rig(axis)
        raw = args[i + 1] if len(args) > i + 1 else 'cfg'
        # 'cfg' means: use whatever the config already has. Lets a macro call
        # this without knowing the driver family or field name.
        sgt = rig.sgt_cfg if raw == 'cfg' else int(raw)
        runs = int(args[i + 2]) if len(args) > i + 2 else 10
        verify(rig, int(sgt), runs, chain=chain)
        title = 'SUMMARY - %s REPEATABILITY' % axis.upper()
    else:
        axis = args[0] if args else 'X'
        rig = Rig(axis)
        # Default to the driver's FULL range, most sensitive end first. The old
        # defaults were hardcoded StallGuard2 numbers, so on a StallGuard4
        # driver they named thresholds that scale does not even contain.
        d_lo, d_hi = min(rig.sg_lo, rig.sg_hi), max(rig.sg_lo, rig.sg_hi)
        if len(args) > 2:
            want_lo, want_hi = int(args[1]), int(args[2])
            lo = max(d_lo, min(d_hi, want_lo))
            hi = max(d_lo, min(d_hi, want_hi))
            if (lo, hi) != (want_lo, want_hi):
                say('  requested %d..%d is outside what %s accepts (%d..%d)'
                    % (want_lo, want_hi, rig.drv, d_lo, d_hi))
                say('  clamped to %d..%d' % (lo, hi))
            step = int(args[3]) if len(args) > 3 else (1 if hi >= lo else -1)
        else:
            lo, hi = int(rig.sg_start), int(rig.sg_end)
            step = 1 if hi >= lo else -1
            say('  no range given - sweeping the full %s range %d..%d'
                % (rig.drv, lo, hi))
        sweep(rig, lo, hi, step, chain=chain)
        title = 'SUMMARY - %s FULL TUNE' % rig.axis
    post('M84')
    flush_summary(title)


if __name__ == '__main__':
    main()
