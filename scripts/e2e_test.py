#!/usr/bin/env python3
"""End-to-end check: 12 agents, coordinator routing, health, backups, gate.

One runnable assert-based self-check (ponytail rule: non-trivial logic leaves one check).
"""
import asyncio
import json
import os
import urllib.request

os.chdir(os.path.expanduser('~/Work/CortexOS'))
import sys
sys.path.insert(0, os.getcwd())

from cortexos.config import Settings
from cortexos.skills import SkillRegistry
from cortexos.agents import AgentCoordinator, AGENT_SPECS
from cortexos.ops import HealthCheck, BackupManager
from cortexos.providers import JevRouter

settings = Settings.load()


def test_agent_count():
    registry = SkillRegistry(settings.skills)
    assert len(registry.list()) == 480, f'expected 480 skills (12 native + 468 imported), got {len(registry.list())}'
    print('PASS: 480 skills registered (12 native + 468 imported)')


def test_coordinator():
    registry = SkillRegistry(settings.skills)
    coord = AgentCoordinator(registry, None)
    assert len(coord.agents) == 12, f'coordinator built {len(coord.agents)} agents'
    assert set(coord.ROLES) == {'research', 'content', 'memory', 'productivity', 'community',
                                'clients', 'sales', 'finance', 'automation', 'ops'}
    print('PASS: coordinator has 12 agents across 10 roles')


def test_jev_routing():
    jev = JevRouter(settings)
    async def run():
        cases = {
            'catch up on AI news': 'research',
            'organize my notes': 'memory',
            'review my expenses': 'finance',
            'plan my week': 'productivity',
        }
        for prompt, expected in cases.items():
            got = await jev.decide('role', prompt)
            assert got == expected, f'{prompt!r} routed to {got}, expected {expected}'
    asyncio.run(run())
    print('PASS: jev routes all 4 roles correctly')


def test_health():
    from cortexos.service import CortexService
    from cortexos.voice import LocalVoice
    svc = CortexService(settings)
    hc = HealthCheck(service=svc, vault=svc.vault, voice=LocalVoice(settings))
    snap = hc.snapshot()
    assert snap['status'] in ('ok', 'degraded'), snap
    assert snap['checks']['skills']['count'] == 480
    print('PASS: health snapshot reports 480 skills')


def test_backup():
    from cortexos.vault import Vault
    vault = Vault(settings.vault)
    bm = BackupManager(vault)
    path = bm.create()
    assert path.exists() and (path / 'state.json').exists(), 'backup missing state.json'
    listed = bm.list()
    assert any(b['name'] == path.name for b in listed), 'backup not listed'
    print('PASS: backup creates restorable copy')


def test_gate():
    from cortexos.service import CortexService
    svc = CortexService(settings)
    async def run():
        import uuid
        r = await svc.submit('send my client an invoice', request_id=f'e2e-gate-{uuid.uuid4().hex[:8]}')
        assert r['status'] == 'awaiting_approval', f'gate did not hold: {r}'
        assert r.get('risk_level') in ('external_action', 'financial', 'unknown')
    asyncio.run(run())
    print('PASS: approval gate holds on send/spend')


if __name__ == '__main__':
    test_agent_count()
    test_coordinator()
    test_health()
    test_backup()
    test_gate()
    try:
        test_jev_routing()
    except AssertionError as exc:
        print(f'FAIL: {exc}')
        raise
    print('\nALL E2E CHECKS PASSED')
