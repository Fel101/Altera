"""Concurrent synthetic image jobs must retain their own references."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest

from core import imagegen


@pytest.mark.parametrize('backend_kind', ['mage', 'workflow'])
def test_overlapping_renders_use_distinct_reference_names(tmp_path, monkeypatch, backend_kind):
    class Comfy:
        models = imagegen.DEFAULT_MODELS

        def __init__(self):
            self.images = {}
            self.lock = threading.Lock()
            self.ready = threading.Barrier(2)

        def upload_image(self, content, name):
            with self.lock:
                self.images[name] = content
            return name

        def run(self, graph):
            self.ready.wait(timeout=5)
            if backend_kind == 'mage':
                names = [graph['20']['inputs']['image'], graph['22']['inputs']['image']]
            else:
                names = [graph['1']['inputs']['first'], graph['1']['inputs']['second']]
            with self.lock:
                return tuple(self.images[name] for name in names)

    comfy = Comfy()
    if backend_kind == 'mage':
        backend = imagegen.MageFlowBackend(comfy)
    else:
        workflow = tmp_path / 'workflow.json'
        workflow.write_text(json.dumps({'1': {'class_type': 'Fixture', 'inputs':
            {'first': '{{reference}}', 'second': '{{reference_2}}'}}}), encoding='utf-8')
        backend = imagegen.WorkflowBackend(comfy, workflow)
        monkeypatch.setattr(imagegen, 'missing_models', lambda *args: [])
        monkeypatch.setattr(imagegen, 'run_guarded', lambda client, graph, _: client.run(graph))
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(backend.render, refs, 'synthetic', '', 1, (512, 512), 1)
                for refs in ([b'companion-a', b'user-a'], [b'companion-b', b'user-b'])]
        assert [job.result(timeout=10) for job in jobs] == [
            (b'companion-a', b'user-a'), (b'companion-b', b'user-b')]
    assert len(comfy.images) == 4
