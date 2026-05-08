from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agentd.service import (
    clear_deferred_service_command,
    deferred_service_request_path,
    read_deferred_service_command,
    write_deferred_service_command,
)


class ServiceRequestTest(unittest.TestCase):
    def test_deferred_service_command_round_trips_request(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            runtime_dir = Path(raw_dir)
            config = SimpleNamespace(runtime_dir=runtime_dir)
            payload = {
                'command': 'restart',
                'backend': 'process',
                'not_before': 123.5,
                'timeout_seconds': 7,
            }

            write_deferred_service_command(config, payload)

            self.assertEqual(read_deferred_service_command(config), payload)
            self.assertTrue(deferred_service_request_path(config).exists())

            clear_deferred_service_command(config)

            self.assertIsNone(read_deferred_service_command(config))


if __name__ == '__main__':
    unittest.main()
