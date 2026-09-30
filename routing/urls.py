from django.urls import path, re_path

from . import views

urlpatterns = [
    path("health/", views.health, name="health"),
    path("route/", views.RouteView.as_view(), name="route"),
    path("route/map/", views.route_map, name="route-map"),
    re_path(r"^(?P<path>.*)$", views.api_not_found),  # last: unknown /api/... -> JSON 404
]
