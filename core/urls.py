from django.urls import path
from . import views

urlpatterns = [
    path('trips/plan/', views.plan_trip_view, name='plan_trip'),
]

app_name = 'core'
