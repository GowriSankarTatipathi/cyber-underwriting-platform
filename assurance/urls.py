from django.urls import path
from . import views

urlpatterns = [
    path('cases/<uuid:case_id>/assessment/', views.overview, name='assessment'),
    path('cases/<uuid:case_id>/assessment/<str:section>/', views.section, name='assessment_section'),
    path('cases/<uuid:case_id>/assessment/edit/<str:action>/', views.edit, name='assessment_edit'),
    path('cases/<uuid:case_id>/assessment/score/<uuid:finding_id>/', views.score, name='assessment_score'),
    path('cases/<uuid:case_id>/assessment/compare/<int:pk>/', views.comparison_review, name='assessment_comparison'),
    path('cases/<uuid:case_id>/assessment/risk/<int:pk>/', views.risk_detail, name='assessment_risk'),
    path('cases/<uuid:case_id>/assessment/quote/<int:pk>/', views.quote_detail, name='assessment_quote'),
    path('cases/<uuid:case_id>/assessment/outage/<int:pk>/', views.outage_detail, name='assessment_outage'),
    path('cases/<uuid:case_id>/assessment/run/<str:action>/', views.run, name='assessment_run'),
    path('cases/<uuid:case_id>/assessment/export/<str:kind>/', views.export, name='assessment_export'),
]
