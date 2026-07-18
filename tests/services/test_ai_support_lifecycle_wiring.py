from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_main_owns_worker_start_and_shutdown() -> None:
    source = (ROOT / 'main.py').read_text(encoding='utf-8')

    assert 'await ai_support_worker_runner.start()' in source
    assert 'await ai_support_worker_runner.stop()' in source
    assert source.index('await ai_support_worker_runner.start()') < source.index(
        'await ai_support_worker_runner.stop()'
    )


def test_unified_health_exposes_worker_status() -> None:
    source = (ROOT / 'app/webserver/unified_app.py').read_text(encoding='utf-8')

    assert "'ai_support_worker': ai_support_worker_runner.get_status()" in source
