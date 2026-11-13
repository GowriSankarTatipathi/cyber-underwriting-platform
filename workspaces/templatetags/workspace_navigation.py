from django import template
from django.urls import reverse
from workspaces.permissions import insurer_role, is_owner, visible_requests

register = template.Library()


@register.inclusion_tag("workspaces/case_navigation.html", takes_context=True)
def case_navigation(context, case):
    request = context["request"]
    role, owner = insurer_role(request.user, case), is_owner(request.user, case)
    links = [("Case overview", reverse("case", args=[case.pk]))]
    if owner or case.consent_active:
        links.append(("Follow-up & decisions" if role else "Follow-up work", reverse("workflow", args=[case.pk])))
        if role or owner or visible_requests(request.user, case).exists():
            links.append(("Services & recovery", reverse("service_inventory", args=[case.pk])))
        if role or owner:
            links.extend([(label, reverse(name, args=[case.pk])) for label, name in (
                ("Portfolio & partners", "portfolio"), ("Monitoring", "monitoring"), ("AI review", "ai_overview"))])
        if role:
            links.append(("Pilot metrics", reverse("pilot_metrics", args=[case.pk])))
    if role:
        links.append(("Renewal history", reverse("case_history", args=[case.pk])))
    # Detail routes belong to their section; exact case path belongs to overview.
    active = max((url for _, url in links if request.path.startswith(url)), key=len, default="")
    name = request.resolver_match.url_name if request.resolver_match else ""
    section = None
    if name.startswith(("capacity_", "portfolio")):
        section = "portfolio"
    elif name.startswith(("finding_", "decision_", "report_", "exception_")):
        section = "workflow"
    elif name.startswith("renewal_"):
        section = "case_history"
    if section:
        target = reverse(section, args=[case.pk])
        if any(url == target for _, url in links):
            active = target
    return {"nav_case": case, "case_links": [{"label": label, "url": url, "active": url == active} for label, url in links]}
