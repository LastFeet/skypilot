"""LastFeet patch: Vast machines that did not come up, left out of offer selection for a while.

Vast's cheapest offer can be a machine that takes the rental and then never finishes loading; cheapest-first selection
then picks it again on every retry (2026-11-24: one 2x RTX 5090 machine, three rentals in a row, each stuck in
"loading" with a download error of its own). `strike()` is called by the provisioner when a new box is still not running
after the load timeout; `blocked()` is read at every offer search.

A block always expires, so the list cannot only grow:
  - 1st incident: 3 days, 2nd: 14 days, 3rd and later: 60 days (BLOCK_DAYS).
  - Failures within SAME_INCIDENT_H hours of the last one are one incident (retries of the same launch).
  - After the block ends the machine is offered again. If it then comes up, `cleared()` forgets it; if it fails again,
    that is the next incident and a longer block.
  - A machine with no failure for FORGET_DAYS starts again from the first incident.
  - At most MAX_ACTIVE machines are blocked at once (the most recently failed ones).
  - When HOST_MACHINES or more machines of one host are blocked, the host's other machines are skipped too, for as
    long as those blocks last (the same day two machines of one host failed the same way, one after the other).

State: ~/.sky/vast_blocked_machines.json (SKY_VAST_BLOCKLIST overrides the path), shared by every launch on this
machine. A task can ignore it with create_instance_kwargs.ignore_machine_blocklist: true.

    python -m sky.provision.vast.blocklist                 # list
    python -m sky.provision.vast.blocklist add ID REASON   # one incident for machine ID
    python -m sky.provision.vast.blocklist remove ID
"""
import fcntl
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Dict, Optional, Set

BLOCK_DAYS = (3, 14, 60)
SAME_INCIDENT_H = 6
FORGET_DAYS = 180
MAX_ACTIVE = 50
HOST_MACHINES = 2
DAY = 86400.0


def _path() -> Path:
    return Path(
        os.environ.get('SKY_VAST_BLOCKLIST') or
        Path.home() / '.sky' / 'vast_blocked_machines.json')


def _load() -> Dict[str, Any]:
    try:
        data = json.loads(_path().read_text(encoding='utf-8'))
        return data if isinstance(data.get('machines'), dict) else {
            'machines': {}
        }
    except (OSError, ValueError, AttributeError):
        return {'machines': {}}


def _update(change) -> None:
    """Read, change and write the file under a lock (several launches run at once)."""
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + '.lock', 'w', encoding='utf-8') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = _load()
        change(data['machines'])
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(data, indent=1), encoding='utf-8')
        os.replace(tmp, path)


def blocked(now: Optional[float] = None) -> Set[int]:
    """Machine ids not to rent right now."""
    now = time.time() if now is None else now
    active = sorted(((m.get('last', 0), int(k))
                     for k, m in _load()['machines'].items()
                     if m.get('until', 0) > now),
                    reverse=True)
    return {k for _, k in active[:MAX_ACTIVE]}


def blocked_hosts(now: Optional[float] = None) -> Set[int]:
    """Hosts with HOST_MACHINES or more machines blocked right now."""
    active = blocked(now)
    count: Dict[int, int] = {}
    for k, m in _load()['machines'].items():
        if int(k) in active and m.get('host_id') is not None:
            count[int(m['host_id'])] = count.get(int(m['host_id']), 0) + 1
    return {h for h, n in count.items() if n >= HOST_MACHINES}


def allowed(offer: Dict[str, Any], machines: Set[int], hosts: Set[int]) -> bool:
    """May this Vast offer be rented, given blocked() and blocked_hosts()?"""
    return (offer.get('machine_id') not in machines and
            offer.get('host_id') not in hosts)


def strike(machine_id: Any,
           reason: str,
           now: Optional[float] = None,
           **info: Any) -> Dict[str, Any]:
    """Record that a machine failed to come up; returns its entry."""
    now = time.time() if now is None else now
    out: Dict[str, Any] = {}

    def change(machines: Dict[str, Any]) -> None:
        m = machines.get(str(machine_id)) or {'strikes': 0, 'history': []}
        last = m.get('last', 0)
        if now - last > FORGET_DAYS * DAY:
            m['strikes'] = 0
        if m['strikes'] == 0 or now - last > SAME_INCIDENT_H * 3600:
            m['strikes'] += 1
        days = BLOCK_DAYS[min(m['strikes'], len(BLOCK_DAYS)) - 1]
        m.update(last=now,
                 until=now + days * DAY,
                 until_date=time.strftime('%Y-%m-%d %H:%M',
                                          time.localtime(now + days * DAY)),
                 reason=reason[:300],
                 **{k: v for k, v in info.items() if v is not None})
        m['history'] = (m.get('history') or [])[-4:] + [{
            'when': time.strftime('%Y-%m-%d %H:%M', time.localtime(now)),
            'reason': reason[:300]
        }]
        machines[str(machine_id)] = m
        out.update(m)

    _update(change)
    return out


def cleared(machine_id: Any) -> bool:
    """A machine came up fine: forget it. True when it was on the list."""
    if str(machine_id) not in _load()['machines']:
        return False
    _update(lambda machines: machines.pop(str(machine_id), None))
    return True


def _main(argv) -> None:
    if argv[:1] == ['add'] and len(argv) >= 2:
        print(strike(argv[1], ' '.join(argv[2:]) or 'added by hand'))
    elif argv[:1] == ['remove'] and len(argv) == 2:
        print('removed' if cleared(argv[1]) else 'not on the list')
    else:
        now, active = time.time(), blocked()
        rows = sorted(_load()['machines'].items(),
                      key=lambda kv: -kv[1].get('last', 0))
        print(f'{len(active)} machine(s) blocked now, {len(rows)} remembered; '
              f'whole hosts blocked: {sorted(blocked_hosts()) or "none"} '
              f'({_path()})')
        for k, m in rows:
            state = (f'blocked until {m.get("until_date")}'
                     if int(k) in active else 'block over: on probation')
            print(f'  machine {k}: incident {m.get("strikes")}, {state}; '
                  f'host {m.get("host_id")}, {m.get("geolocation") or ""} '
                  f'{m.get("reason", "")[:110]}')
        del now


if __name__ == '__main__':
    _main(sys.argv[1:])
