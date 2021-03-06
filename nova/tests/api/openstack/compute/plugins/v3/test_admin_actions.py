#   Copyright 2011 OpenStack Foundation
#
#   Licensed under the Apache License, Version 2.0 (the "License"); you may
#   not use this file except in compliance with the License. You may obtain
#   a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#   WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#   License for the specific language governing permissions and limitations
#   under the License.

import webob

from nova.api.openstack import common
from nova.api.openstack.compute.plugins.v3 import admin_actions
from nova.compute import vm_states
import nova.context
from nova import exception
from nova.objects import instance as instance_obj
from nova.openstack.common import jsonutils
from nova.openstack.common import timeutils
from nova.openstack.common import uuidutils
from nova import test
from nova.tests.api.openstack import fakes
from nova.tests import fake_instance


class CommonMixin(object):
    def setUp(self):
        super(CommonMixin, self).setUp()
        self.controller = admin_actions.AdminActionsController()
        self.compute_api = self.controller.compute_api
        self.context = nova.context.RequestContext('fake', 'fake')

        def _fake_controller(*args, **kwargs):
            return self.controller

        self.stubs.Set(admin_actions, 'AdminActionsController',
                       _fake_controller)

        self.app = fakes.wsgi_app_v3(init_only=('servers',
                                                'os-admin-actions'),
                                     fake_auth_context=self.context)
        self.mox.StubOutWithMock(self.compute_api, 'get')

    def _make_request(self, url, body):
        req = webob.Request.blank('/v3' + url)
        req.method = 'POST'
        req.body = jsonutils.dumps(body)
        req.content_type = 'application/json'
        return req.get_response(self.app)

    def _stub_instance_get(self, uuid=None, objects=True):
        if uuid is None:
            uuid = uuidutils.generate_uuid()
        instance = fake_instance.fake_db_instance(
                id=1, uuid=uuid, vm_state=vm_states.ACTIVE,
                task_state=None, launched_at=timeutils.utcnow())
        if objects:
            instance = instance_obj.Instance._from_db_object(
                    self.context, instance_obj.Instance(), instance)
            self.compute_api.get(self.context, uuid,
                                 want_objects=True).AndReturn(instance)
        else:
            self.compute_api.get(self.context, uuid).AndReturn(instance)
        return instance

    def _stub_instance_get_failure(self, exc_info, uuid=None, objects=True):
        if uuid is None:
            uuid = uuidutils.generate_uuid()
        if objects:
            self.compute_api.get(self.context, uuid,
                                 want_objects=True).AndRaise(exc_info)
        else:
            self.compute_api.get(self.context, uuid).AndRaise(exc_info)
        return uuid

    def _test_non_existing_instance(self, action, body_map=None,
                                    objects=True):
        uuid = uuidutils.generate_uuid()
        self._stub_instance_get_failure(
                exception.InstanceNotFound(instance_id=uuid),
                uuid=uuid, objects=objects)

        self.mox.ReplayAll()

        res = self._make_request('/servers/%s/action' % uuid,
                                 {action: body_map.get(action)})
        self.assertEqual(404, res.status_int)
        # Do these here instead of tearDown because this method is called
        # more than once for the same test case
        self.mox.VerifyAll()
        self.mox.UnsetStubs()

    def _test_action(self, action, body=None, method=None, objects=True):
        if method is None:
            method = action

        instance = self._stub_instance_get(objects=objects)
        getattr(self.compute_api, method)(self.context, instance)

        self.mox.ReplayAll()

        res = self._make_request('/servers/%s/action' % instance['uuid'],
                                 {action: None})
        self.assertEqual(202, res.status_int)
        # Do these here instead of tearDown because this method is called
        # more than once for the same test case
        self.mox.VerifyAll()
        self.mox.UnsetStubs()

    def _test_invalid_state(self, action, method=None, body_map=None,
                            compute_api_args_map=None,
                            objects=True):
        if method is None:
            method = action
        if body_map is None:
            body_map = {}
        if compute_api_args_map is None:
            compute_api_args_map = {}

        instance = self._stub_instance_get(objects=objects)

        args, kwargs = compute_api_args_map.get(action, ((), {}))

        getattr(self.compute_api, method)(self.context, instance,
                                          *args, **kwargs).AndRaise(
                exception.InstanceInvalidState(
                    attr='vm_state', instance_uuid=instance['uuid'],
                    state='foo', method=method))

        self.mox.ReplayAll()

        res = self._make_request('/servers/%s/action' % instance['uuid'],
                                 {action: body_map.get(action)})
        self.assertEqual(409, res.status_int)
        self.assertIn("Cannot \'%s\' while instance" % action, res.body)
        # Do these here instead of tearDown because this method is called
        # more than once for the same test case
        self.mox.VerifyAll()
        self.mox.UnsetStubs()

    def _test_locked_instance(self, action, method=None, objects=True):
        if method is None:
            method = action

        instance = self._stub_instance_get(objects=objects)
        getattr(self.compute_api, method)(self.context, instance).AndRaise(
                exception.InstanceIsLocked(instance_uuid=instance['uuid']))

        self.mox.ReplayAll()

        res = self._make_request('/servers/%s/action' % instance['uuid'],
                                 {action: None})
        self.assertEqual(409, res.status_int)
        # Do these here instead of tearDown because this method is called
        # more than once for the same test case
        self.mox.VerifyAll()
        self.mox.UnsetStubs()


class AdminActionsTest(CommonMixin, test.TestCase):
    def test_actions(self):
        actions = ['pause', 'unpause', 'suspend', 'resume', 'migrate',
                   'reset_network', 'inject_network_info', 'lock',
                   'unlock']
        actions_not_objectified = ['migrate', 'reset_network', 'lock',
                                   'unlock', 'inject_network_info']
        method_translations = {'migrate': 'resize'}

        for action in actions:
            old_style = action in actions_not_objectified
            method = method_translations.get(action)
            self.mox.StubOutWithMock(self.compute_api, method or action)
            self._test_action(action, method=method, objects=not old_style)
            # Re-mock this.
            self.mox.StubOutWithMock(self.compute_api, 'get')

    def test_actions_raise_conflict_on_invalid_state(self):
        actions = ['pause', 'unpause', 'suspend', 'resume', 'migrate',
                   'migrate_live']
        actions_not_objectified = ['migrate', 'migrate_live']
        method_translations = {'migrate': 'resize',
                               'migrate_live': 'live_migrate'}

        body_map = {'migrate_live': {'host': 'hostname',
                                     'block_migration': False,
                                     'disk_over_commit': False}}
        args_map = {'migrate_live': ((False, False, 'hostname'), {})}

        for action in actions:
            old_style = action in actions_not_objectified
            method = method_translations.get(action)
            self.mox.StubOutWithMock(self.compute_api, method or action)
            self._test_invalid_state(action, method=method,
                                     body_map=body_map,
                                     compute_api_args_map=args_map,
                                     objects=not old_style)
            # Re-mock this.
            self.mox.StubOutWithMock(self.compute_api, 'get')

    def test_actions_with_non_existed_instance(self):
        actions = ['pause', 'unpause', 'suspend', 'resume', 'migrate',
                   'reset_network', 'inject_network_info', 'lock',
                   'unlock', 'reset_state', 'migrate_live']
        actions_not_objectified = ['migrate', 'reset_network', 'lock',
                                   'unlock', 'inject_network_info',
                                   'migrate_live']
        body_map = {'reset_state': {'state': 'active'},
                    'migrate_live': {'host': 'hostname',
                                     'block_migration': False,
                                     'disk_over_commit': False}}
        for action in actions:
            old_style = action in actions_not_objectified
            self._test_non_existing_instance(action,
                                             body_map=body_map,
                                             objects=not old_style)
            # Re-mock this.
            self.mox.StubOutWithMock(self.compute_api, 'get')

    def test_actions_with_locked_instance(self):
        actions = ['pause', 'unpause', 'suspend', 'resume', 'migrate',
                   'reset_network', 'inject_network_info']
        method_translations = {'migrate': 'resize'}
        actions_not_objectified = ['migrate', 'reset_network',
                                   'inject_network_info']

        for action in actions:
            old_style = action in actions_not_objectified
            method = method_translations.get(action)
            self.mox.StubOutWithMock(self.compute_api, method or action)
            self._test_locked_instance(action, method=method,
                                       objects=not old_style)
            # Re-mock this.
            self.mox.StubOutWithMock(self.compute_api, 'get')

    def _test_migrate_exception(self, exc_info, expected_result):
        self.mox.StubOutWithMock(self.compute_api, 'resize')
        instance = self._stub_instance_get(objects=False)
        self.compute_api.resize(self.context, instance).AndRaise(exc_info)

        self.mox.ReplayAll()

        res = self._make_request('/servers/%s/action' % instance['uuid'],
                                 {'migrate': None})
        self.assertEqual(expected_result, res.status_int)

    def test_migrate_resize_to_same_flavor(self):
        exc_info = exception.CannotResizeToSameFlavor()
        self._test_migrate_exception(exc_info, 400)

    def test_migrate_too_many_instances(self):
        exc_info = exception.TooManyInstances(overs='', req='', used=0,
                                              allowed=0, resource='')
        self._test_migrate_exception(exc_info, 413)

    def test_migrate_live_enabled(self):
        self.mox.StubOutWithMock(self.compute_api, 'live_migrate')
        instance = self._stub_instance_get(objects=False)
        self.compute_api.live_migrate(self.context, instance, False,
                                      False, 'hostname')

        self.mox.ReplayAll()

        res = self._make_request('/servers/%s/action' % instance['uuid'],
                                 {'migrate_live':
                                  {'host': 'hostname',
                                   'block_migration': False,
                                   'disk_over_commit': False}})
        self.assertEqual(202, res.status_int)

    def test_migrate_live_missing_dict_param(self):
        res = self._make_request('/servers/FAKE/action',
                                 {'migrate_live': {'dummy': 'hostname',
                                                   'block_migration': False,
                                                   'disk_over_commit': False}})
        self.assertEqual(400, res.status_int)

    def _test_migrate_live_failed_with_exception(self, fake_exc,
                                                 uuid=None):
        self.mox.StubOutWithMock(self.compute_api, 'live_migrate')

        instance = self._stub_instance_get(uuid=uuid, objects=False)
        self.compute_api.live_migrate(self.context, instance, False,
                                      False, 'hostname').AndRaise(fake_exc)

        self.mox.ReplayAll()

        res = self._make_request('/servers/%s/action' % instance['uuid'],
                                 {'migrate_live':
                                  {'host': 'hostname',
                                   'block_migration': False,
                                   'disk_over_commit': False}})
        self.assertEqual(400, res.status_int)
        self.assertIn(unicode(fake_exc), res.body)

    def test_migrate_live_compute_service_unavailable(self):
        self._test_migrate_live_failed_with_exception(
            exception.ComputeServiceUnavailable(host='host'))

    def test_migrate_live_invalid_hypervisor_type(self):
        self._test_migrate_live_failed_with_exception(
            exception.InvalidHypervisorType())

    def test_migrate_live_unable_to_migrate_to_self(self):
        uuid = uuidutils.generate_uuid()
        self._test_migrate_live_failed_with_exception(
                exception.UnableToMigrateToSelf(instance_id=uuid,
                                                host='host'),
                uuid=uuid)

    def test_migrate_live_destination_hypervisor_too_old(self):
        self._test_migrate_live_failed_with_exception(
            exception.DestinationHypervisorTooOld())

    def test_migrate_live_no_valid_host(self):
        self._test_migrate_live_failed_with_exception(
            exception.NoValidHost(reason=''))

    def test_migrate_live_invalid_local_storage(self):
        self._test_migrate_live_failed_with_exception(
            exception.InvalidLocalStorage(path='', reason=''))

    def test_migrate_live_invalid_shared_storage(self):
        self._test_migrate_live_failed_with_exception(
            exception.InvalidSharedStorage(path='', reason=''))

    def test_migrate_live_pre_check_error(self):
        self._test_migrate_live_failed_with_exception(
            exception.MigrationPreCheckError(reason=''))

    def test_unlock_not_authorized(self):
        self.mox.StubOutWithMock(self.compute_api, 'unlock')

        instance = self._stub_instance_get(objects=False)

        self.compute_api.unlock(self.context, instance).AndRaise(
                exception.PolicyNotAuthorized(action='unlock'))

        self.mox.ReplayAll()

        res = self._make_request('/servers/%s/action' % instance['uuid'],
                                 {'unlock': None})
        self.assertEqual(403, res.status_int)


class CreateBackupTests(CommonMixin, test.TestCase):
    def setUp(self):
        super(CreateBackupTests, self).setUp()
        self.mox.StubOutWithMock(common,
                                 'check_img_metadata_properties_quota')
        self.mox.StubOutWithMock(self.compute_api,
                                 'backup')

    def _make_url(self, uuid):
        return '/servers/%s/action' % uuid

    def test_create_backup_with_metadata(self):
        metadata = {'123': 'asdf'}
        body = {
            'create_backup': {
                'name': 'Backup 1',
                'backup_type': 'daily',
                'rotation': 1,
                'metadata': metadata,
            },
        }

        image = dict(id='fake-image-id', status='ACTIVE', name='Backup 1',
                     properties=metadata)

        common.check_img_metadata_properties_quota(self.context, metadata)
        instance = self._stub_instance_get(objects=False)
        self.compute_api.backup(self.context, instance, 'Backup 1',
                                'daily', 1,
                                extra_properties=metadata).AndReturn(image)

        self.mox.ReplayAll()

        res = self._make_request(self._make_url(instance['uuid']), body)
        self.assertEqual(202, res.status_int)
        self.assertIn('fake-image-id', res.headers['Location'])

    def test_create_backup_no_name(self):
        # Name is required for backups.
        body = {
            'create_backup': {
                'backup_type': 'daily',
                'rotation': 1,
            },
        }
        res = self._make_request(self._make_url('fake'), body)
        self.assertEqual(400, res.status_int)

    def test_create_backup_no_rotation(self):
        # Rotation is required for backup requests.
        body = {
            'create_backup': {
                'name': 'Backup 1',
                'backup_type': 'daily',
            },
        }
        res = self._make_request(self._make_url('fake'), body)
        self.assertEqual(400, res.status_int)

    def test_create_backup_negative_rotation(self):
        """Rotation must be greater than or equal to zero
        for backup requests
        """
        body = {
            'create_backup': {
                'name': 'Backup 1',
                'backup_type': 'daily',
                'rotation': -1,
            },
        }
        res = self._make_request(self._make_url('fake'), body)
        self.assertEqual(400, res.status_int)

    def test_create_backup_no_backup_type(self):
        # Backup Type (daily or weekly) is required for backup requests.
        body = {
            'create_backup': {
                'name': 'Backup 1',
                'rotation': 1,
            },
        }
        res = self._make_request(self._make_url('fake'), body)
        self.assertEqual(400, res.status_int)

    def test_create_backup_bad_entity(self):
        body = {'create_backup': 'go'}
        res = self._make_request(self._make_url('fake'), body)
        self.assertEqual(400, res.status_int)

    def test_create_backup_rotation_is_zero(self):
        # The happy path for creating backups if rotation is zero.
        body = {
            'create_backup': {
                'name': 'Backup 1',
                'backup_type': 'daily',
                'rotation': 0,
            },
        }

        image = dict(id='fake-image-id', status='ACTIVE', name='Backup 1',
                     properties={})
        common.check_img_metadata_properties_quota(self.context, {})
        instance = self._stub_instance_get(objects=False)
        self.compute_api.backup(self.context, instance, 'Backup 1',
                                'daily', 0,
                                extra_properties={}).AndReturn(image)

        self.mox.ReplayAll()

        res = self._make_request(self._make_url(instance['uuid']), body)
        self.assertEqual(202, res.status_int)
        self.assertNotIn('Location', res.headers)

    def test_create_backup_rotation_is_positive(self):
        # The happy path for creating backups if rotation is positive.
        body = {
            'create_backup': {
                'name': 'Backup 1',
                'backup_type': 'daily',
                'rotation': 1,
            },
        }

        image = dict(id='fake-image-id', status='ACTIVE', name='Backup 1',
                     properties={})
        common.check_img_metadata_properties_quota(self.context, {})
        instance = self._stub_instance_get(objects=False)
        self.compute_api.backup(self.context, instance, 'Backup 1',
                                'daily', 1,
                                extra_properties={}).AndReturn(image)

        self.mox.ReplayAll()

        res = self._make_request(self._make_url(instance['uuid']), body)
        self.assertEqual(202, res.status_int)
        self.assertIn('fake-image-id', res.headers['Location'])

    def test_create_backup_raises_conflict_on_invalid_state(self):
        body_map = {
            'create_backup': {
                'name': 'Backup 1',
                'backup_type': 'daily',
                'rotation': 1,
            },
        }
        args_map = {
            'create_backup': (
                ('Backup 1', 'daily', 1), {'extra_properties': {}}
            ),
        }
        common.check_img_metadata_properties_quota(self.context, {})
        self._test_invalid_state('create_backup', method='backup',
                                 body_map=body_map,
                                 compute_api_args_map=args_map,
                                 objects=False)

    def test_create_backup_with_non_existed_instance(self):
        body_map = {
            'create_backup': {
                'name': 'Backup 1',
                'backup_type': 'daily',
                'rotation': 1,
            },
        }
        common.check_img_metadata_properties_quota(self.context, {})
        self._test_non_existing_instance('create_backup',
                                         body_map=body_map,
                                         objects=False)


class ResetStateTests(test.TestCase):
    def setUp(self):
        super(ResetStateTests, self).setUp()

        self.uuid = uuidutils.generate_uuid()

        self.admin_api = admin_actions.AdminActionsController()
        self.compute_api = self.admin_api.compute_api

        url = '/servers/%s/action' % self.uuid
        self.request = fakes.HTTPRequestV3.blank(url)
        self.context = self.request.environ['nova.context']

    def test_no_state(self):
        self.assertRaises(webob.exc.HTTPBadRequest,
                          self.admin_api._reset_state,
                          self.request, self.uuid,
                          {"reset_state": None})

    def test_bad_state(self):
        self.assertRaises(webob.exc.HTTPBadRequest,
                          self.admin_api._reset_state,
                          self.request, self.uuid,
                          {"reset_state": {"state": "spam"}})

    def test_no_instance(self):
        self.mox.StubOutWithMock(self.compute_api, 'get')
        exc = exception.InstanceNotFound(instance_id='inst_ud')
        self.compute_api.get(self.context, self.uuid,
                             want_objects=True).AndRaise(exc)

        self.mox.ReplayAll()

        self.assertRaises(webob.exc.HTTPNotFound,
                          self.admin_api._reset_state,
                          self.request, self.uuid,
                          {"reset_state": {"state": "active"}})

    def _setup_mock(self, expected):
        instance = instance_obj.Instance()
        instance.uuid = self.uuid
        instance.vm_state = 'fake'
        instance.task_state = 'fake'
        instance.obj_reset_changes()

        self.mox.StubOutWithMock(instance, 'save')
        self.mox.StubOutWithMock(self.compute_api, 'get')

        def check_state(admin_state_reset=True):
            self.assertEqual(set(expected.keys()),
                             instance.obj_what_changed())
            for k, v in expected.items():
                self.assertEqual(v, getattr(instance, k),
                                 "Instance.%s doesn't match" % k)
            instance.obj_reset_changes()

        self.compute_api.get(self.context, instance.uuid,
                             want_objects=True).AndReturn(instance)
        instance.save(admin_state_reset=True).WithSideEffects(check_state)

    def test_reset_active(self):
        self._setup_mock(dict(vm_state=vm_states.ACTIVE,
                              task_state=None))
        self.mox.ReplayAll()

        body = {"reset_state": {"state": "active"}}
        result = self.admin_api._reset_state(self.request, self.uuid, body)

        self.assertEqual(202, result.status_int)

    def test_reset_error(self):
        self._setup_mock(dict(vm_state=vm_states.ERROR,
                              task_state=None))
        self.mox.ReplayAll()
        body = {"reset_state": {"state": "error"}}
        result = self.admin_api._reset_state(self.request, self.uuid, body)

        self.assertEqual(202, result.status_int)
