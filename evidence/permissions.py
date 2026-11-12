from django.http import Http404
from workspaces.models import EvidenceRequest
from workspaces.permissions import get_case, insurer_role, is_owner, require_evidence_access, visible_requests


def require_reviewer(user, case):
    require_evidence_access(user, case)
    if not insurer_role(user, case):
        raise Http404


def require_inventory_editor(user, case):
    require_evidence_access(user, case)
    if not is_owner(user, case) and not insurer_role(user, case):
        raise Http404


def get_request(user, case_id, request_id):
    case = get_case(user, case_id)
    try:
        item = visible_requests(user, case).get(pk=request_id)
    except EvidenceRequest.DoesNotExist:
        raise Http404
    return case, item


def require_version(user, version):
    case, item = get_request(user, version.document.request.case_id, version.document.request_id)
    return case
