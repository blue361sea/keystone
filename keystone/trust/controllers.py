# Copyright 2013 OpenStack Foundation
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import uuid

from oslo_utils import timeutils
import six

from keystone import assignment
from keystone.common import controller
from keystone.common import dependency
from keystone.common import utils
from keystone.common import validation
from keystone import exception
from keystone.i18n import _
from keystone import notifications
from keystone.trust import schema


def _trustor_trustee_only(trust, user_id):
    if user_id not in [trust.get('trustee_user_id'),
                       trust.get('trustor_user_id')]:
        raise exception.Forbidden()


@dependency.requires('assignment_api', 'identity_api', 'resource_api',
                     'role_api', 'token_provider_api', 'trust_api')
class TrustV3(controller.V3Controller):
    collection_name = "trusts"
    member_name = "trust"

    @classmethod
    def base_url(cls, context, path=None):
        """Construct a path and pass it to V3Controller.base_url method."""
        # NOTE(stevemar): Overriding path to /OS-TRUST/trusts so that
        # V3Controller.base_url handles setting the self link correctly.
        path = '/OS-TRUST/' + cls.collection_name
        return super(TrustV3, cls).base_url(context, path=path)

    def get_trust(self, request, trust_id):
        trust = self.trust_api.get_trust(trust_id)
        _trustor_trustee_only(trust, request.context.user_id)
        self._fill_in_roles(request.context_dict, trust,
                            self.role_api.list_roles())
        return TrustV3.wrap_member(request.context_dict, trust)

    def _fill_in_roles(self, context, trust, all_roles):
        if trust.get('expires_at') is not None:
            trust['expires_at'] = (utils.isotime
                                   (trust['expires_at'],
                                    subsecond=True))

        if 'roles' not in trust:
            trust['roles'] = []
        trust_full_roles = []
        for trust_role in trust['roles']:
            if isinstance(trust_role, six.string_types):
                trust_role = {'id': trust_role}
            matching_roles = [x for x in all_roles
                              if x['id'] == trust_role['id']]
            if matching_roles:
                full_role = assignment.controllers.RoleV3.wrap_member(
                    context, matching_roles[0])['role']
                trust_full_roles.append(full_role)
        trust['roles'] = trust_full_roles
        trust['roles_links'] = {
            'self': (self.base_url(context) + "/%s/roles" % trust['id']),
            'next': None,
            'previous': None}

    def _normalize_role_list(self, trust, all_roles):
        trust_roles = []
        all_role_names = {r['name']: r for r in all_roles}
        for role in trust.get('roles', []):
            if 'id' in role:
                trust_roles.append({'id': role['id']})
            elif 'name' in role:
                rolename = role['name']
                if rolename in all_role_names:
                    trust_roles.append({'id':
                                        all_role_names[rolename]['id']})
                else:
                    raise exception.RoleNotFound(_("role %s is not defined") %
                                                 rolename)
            else:
                raise exception.ValidationError(attribute='id or name',
                                                target='roles')
        return trust_roles

    @controller.protected()
    def create_trust(self, request, trust):
        """Create a new trust.

        The user creating the trust must be the trustor.

        """
        validation.lazy_validate(schema.trust_create, trust)
        # Check if delegated via trust
        if request.context.is_delegated_auth:
            # Redelegation case
            src_trust_id = request.context.trust_id
            if not src_trust_id:
                raise exception.Forbidden(
                    _('Redelegation allowed for delegated by trust only'))

            redelegated_trust = self.trust_api.get_trust(src_trust_id)
        else:
            redelegated_trust = None

        if trust.get('project_id') and not trust.get('roles'):
            msg = _('At least one role should be specified.')
            raise exception.Forbidden(msg)

        # the creating user must be the trustor
        if request.context.user_id != trust.get('trustor_user_id'):
            msg = _("The authenticated user should match the trustor.")
            raise exception.Forbidden(msg)

        # ensure trustee exists
        self.identity_api.get_user(trust['trustee_user_id'])

        all_roles = self.role_api.list_roles()
        # Normalize roles
        normalized_roles = self._normalize_role_list(trust, all_roles)
        trust['roles'] = normalized_roles
        self._require_trustor_has_role_in_project(trust)
        trust['expires_at'] = self._parse_expiration_date(
            trust.get('expires_at'))
        trust_id = uuid.uuid4().hex
        initiator = notifications._get_request_audit_info(request.context_dict)
        new_trust = self.trust_api.create_trust(trust_id, trust,
                                                normalized_roles,
                                                redelegated_trust,
                                                initiator)
        self._fill_in_roles(request.context_dict, new_trust, all_roles)
        return TrustV3.wrap_member(request.context_dict, new_trust)

    def _get_trustor_roles(self, trust):
        original_trust = trust.copy()
        while original_trust.get('redelegated_trust_id'):
            original_trust = self.trust_api.get_trust(
                original_trust['redelegated_trust_id'])

        if not self._attribute_is_empty(trust, 'project_id'):
            self.resource_api.get_project(original_trust['project_id'])
            # Get a list of roles including any domain specific roles
            assignment_list = self.assignment_api.list_role_assignments(
                user_id=original_trust['trustor_user_id'],
                project_id=original_trust['project_id'],
                effective=True, strip_domain_roles=False)
            return list(set([x['role_id'] for x in assignment_list]))
        else:
            return []

    def _require_trustor_has_role_in_project(self, trust):
        trustor_roles = self._get_trustor_roles(trust)
        for trust_role in trust['roles']:
            matching_roles = [x for x in trustor_roles
                              if x == trust_role['id']]
            if not matching_roles:
                raise exception.RoleNotFound(role_id=trust_role['id'])

    def _parse_expiration_date(self, expiration_date):
        if expiration_date is None:
            return None
        if not expiration_date.endswith('Z'):
            expiration_date += 'Z'
        try:
            expiration_time = timeutils.parse_isotime(expiration_date)
        except ValueError:
            raise exception.ValidationTimeStampError()
        if timeutils.is_older_than(expiration_time, 0):
            raise exception.ValidationExpirationError()
        return expiration_time

    @controller.protected()
    def list_trusts(self, request):
        trusts = []
        trustor_user_id = request.params.get('trustor_user_id')
        trustee_user_id = request.params.get('trustee_user_id')

        if not request.params:
            self.assert_admin(request)
            trusts += self.trust_api.list_trusts()

        if trustor_user_id:
            if trustor_user_id != request.context.user_id:
                raise exception.Forbidden()

            trusts += self.trust_api.list_trusts_for_trustor(trustor_user_id)

        if trustee_user_id:
            if trustee_user_id != request.context.user_id:
                raise exception.Forbidden()

            trusts += self.trust_api.list_trusts_for_trustee(trustee_user_id)

        for trust in trusts:
            # get_trust returns roles, list_trusts does not
            # It seems in some circumstances, roles does not
            # exist in the query response, so check first
            if 'roles' in trust:
                del trust['roles']

            if trust.get('expires_at') is not None:
                trust['expires_at'] = utils.isotime(trust['expires_at'],
                                                    subsecond=True)

        return TrustV3.wrap_collection(request.context_dict, trusts)

    @controller.protected()
    def delete_trust(self, request, trust_id):
        trust = self.trust_api.get_trust(trust_id)

        if (request.context.user_id != trust.get('trustor_user_id') and
                not request.context.is_admin):
            raise exception.Forbidden()

        initiator = notifications._get_request_audit_info(request.context_dict)
        self.trust_api.delete_trust(trust_id, initiator)

    @controller.protected()
    def list_roles_for_trust(self, request, trust_id):
        trust = self.get_trust(request, trust_id)['trust']
        _trustor_trustee_only(trust, request.context.user_id)
        return {'roles': trust['roles'],
                'links': trust['roles_links']}

    @controller.protected()
    def get_role_for_trust(self, request, trust_id, role_id):
        """Get a role that has been assigned to a trust."""
        trust = self.trust_api.get_trust(trust_id)
        _trustor_trustee_only(trust, request.context.user_id)

        if not any(role['id'] == role_id for role in trust['roles']):
            raise exception.RoleNotFound(role_id=role_id)

        role = self.role_api.get_role(role_id)
        return assignment.controllers.RoleV3.wrap_member(request.context_dict,
                                                         role)
