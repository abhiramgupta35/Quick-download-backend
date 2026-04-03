from django.urls import path
from .views import FetchInfoView, DownloadView

urlpatterns = [
    path('fetch-info/', FetchInfoView.as_view(), name='fetch-info'),
    path('download/', DownloadView.as_view(), name='download'),
]
