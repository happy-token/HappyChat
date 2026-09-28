from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import model_policy
from happy_token_gateway import HappyTokenGateway, GatewayRequestError

CATALOG = [
    {'id': 'default::luna', 'group': 'default', 'upstream_model': 'luna'},
    {'id': 'pro::astra', 'group': 'pro', 'upstream_model': 'astra'},
    {'id': 'pro::luna', 'group': 'pro', 'upstream_model': 'luna'},
]
POLICY = {'configured': True, 'groups': [
    {'id': 'pro', 'enabled': True, 'models': ['pro::luna']},
    {'id': 'default', 'enabled': False, 'models': None},
], 'default_model': 'pro::luna'}


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'HAPPYCHAT_MODEL_POLICY_PATH': str(Path(self.directory.name) / 'policy.json')})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.directory.cleanup()

    def test_persisted_rules_filter_disabled_groups_models_and_new_groups(self):
        model_policy.save(POLICY)
        self.assertEqual(model_policy.load(), POLICY)
        self.assertEqual([m['id'] for m in model_policy.apply(CATALOG + [{'id': 'new::model'}])], ['pro::luna'])

    def test_group_order_applies_to_open_webui_items_without_group_metadata(self):
        policy = dict(POLICY, groups=[{'id': 'pro', 'enabled': True, 'models': None}, {'id': 'default', 'enabled': True, 'models': None}])
        self.assertEqual([m['id'] for m in model_policy.apply([{'id': m['id']} for m in CATALOG], policy)], ['pro::astra', 'pro::luna', 'default::luna'])

    def test_invalid_update_preserves_previous_configuration(self):
        model_policy.save(POLICY)
        with self.assertRaises(ValueError):
            model_policy.save(dict(POLICY, groups=[{'id': 'pro', 'enabled': 'false', 'models': None}]))
        self.assertEqual(model_policy.load(), POLICY)

    def test_policy_applies_to_cached_gateway_catalog_and_blocks_actual_chat(self):
        gateway = HappyTokenGateway(configured_models=['allowed', 'blocked'], static_api_key='test', api_base_url='https://example.invalid/v1')
        policy = {'configured': True, 'groups': [{'id': 'default', 'enabled': True, 'models': ['allowed']}], 'default_model': 'allowed'}
        model_policy.save(policy)
        self.assertEqual([m['id'] for m in gateway.model_catalog()], ['allowed'])
        self.assertEqual(len(gateway.model_catalog(apply_policy=False)), 2)
        with self.assertRaises(GatewayRequestError) as context:
            gateway.open_model_request(None, path='/chat/completions', body=b'{"model":"blocked"}', content_type='application/json', accept='application/json')
        self.assertEqual(context.exception.status, 403)
        policy['groups'][0]['enabled'] = False
        model_policy.save(policy)
        self.assertEqual(gateway.model_catalog(), [])


class AdminHTTPTests(PolicyTests):
    def setUp(self):
        super().setUp()
        with patch.dict(os.environ, {'OPEN_WEBUI_URL': 'http://127.0.0.1:1'}):
            import control_proxy
        class Handler(control_proxy.Handler):
            def _request_upstream(self, method=None, path=None, body=None):
                response = io.BytesIO(json.dumps(
                    {'role': 'admin' if (self.headers.get('Authorization') == 'Bearer admin-test' or self.headers.get('Cookie') == 'token=admin-test') else 'user'} if path == '/api/v1/auths/' else
                    {'default_models': 'default::luna'} if self.path == '/api/config' else {'data': CATALOG}
                ).encode())
                response.status = 200
                response.headers = {'Content-Type': 'application/json'}
                return response
        gateway = types.SimpleNamespace(model_catalog=lambda **kwargs: CATALOG, invalidate_model_catalog=lambda: None)
        self.adapter = patch.dict(sys.modules, {'model_adapter': types.SimpleNamespace(HAPPY_TOKEN=gateway)})
        self.adapter.start()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.adapter.stop()
        super().tearDown()

    def request(self, path='/api/happychat/admin/models', body=None, token='admin-test', cookie=None, origin=None):
        headers = {'Content-Type': 'application/json'}
        if origin:
            headers['Origin'] = origin
        if cookie:
            headers['Cookie'] = cookie
        if token:
            headers['Authorization'] = f'Bearer {token}'
        request = urllib.request.Request(f'http://127.0.0.1:{self.server.server_port}{path}', data=json.dumps(body).encode() if body is not None else None, headers=headers, method='PUT' if body is not None else 'GET')
        try:
            return urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as error:
            return error

    def test_only_admin_can_read_or_write(self):
        for token in (None, 'user-test'):
            for body in (None, POLICY):
                with self.request(body=body, token=token) as response:
                    self.assertEqual(response.status, 403)
        self.assertFalse(model_policy.load()['configured'])

    def test_save_changes_chat_list_and_default_config(self):
        with self.request(body=POLICY) as response:
            self.assertEqual(response.status, 200)
        with self.request('/api/models', token='user-test') as response:
            self.assertEqual([m['id'] for m in json.load(response)['data']], ['pro::luna'])
        with self.request('/api/config', token='user-test') as response:
            self.assertEqual(json.load(response)['default_models'], 'pro::luna')
        with self.request() as response:
            self.assertEqual(json.load(response)['policy'], POLICY)

    def test_disabled_default_rejected_and_previous_settings_preserved(self):
        model_policy.save(POLICY)
        with self.request(body=dict(POLICY, default_model='default::luna')) as response:
            self.assertEqual(response.status, 400)
        self.assertEqual(model_policy.load(), POLICY)

    def test_cookie_only_admin_cannot_mutate_settings(self):
        with self.request(token=None, cookie='token=admin-test') as response:
            self.assertEqual(response.status, 200)
        with self.request(body=POLICY, token=None, cookie='token=admin-test') as response:
            self.assertEqual(response.status, 403)
        self.assertFalse(model_policy.load()['configured'])

    def test_foreign_origin_cannot_mutate_even_with_admin_bearer(self):
        with self.request(body=POLICY, origin='https://other.example') as response:
            self.assertEqual(response.status, 403)
        self.assertFalse(model_policy.load()['configured'])
