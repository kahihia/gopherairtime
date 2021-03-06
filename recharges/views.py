from django.contrib.auth.models import User, Group
from .models import Recharge
from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated
from recharges.serializers import (UserSerializer, GroupSerializer,
                                   RechargeSerializer)


class UserViewSet(viewsets.ModelViewSet):

    """
    API endpoint that allows users to be viewed or edited.
    """
    queryset = User.objects.all()
    serializer_class = UserSerializer


class GroupViewSet(viewsets.ModelViewSet):

    """
    API endpoint that allows groups to be viewed or edited.
    """
    queryset = Group.objects.all()
    serializer_class = GroupSerializer


class RechargeViewSet(viewsets.ModelViewSet):

    """
    API endpoint that allows dummy models to be viewed or edited.
    """
    permission_classes = (IsAuthenticated,)
    queryset = Recharge.objects.all()
    serializer_class = RechargeSerializer
