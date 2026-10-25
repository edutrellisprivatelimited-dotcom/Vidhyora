from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.db.models import F
from django.dispatch import receiver

from .devices import forget_device, register_device
from .models import StoreProfile


@receiver(user_logged_in)
def claim_device_login(sender, request, user, **kwargs):
    if request is None:
        return
    if not request.session.session_key:
        request.session.save()
    # myapp.device_links sets this while logging a device in with a link.
    register_device(user, request.session.session_key, request,
                    via_link=getattr(request, 'device_login_link', False))
    profile, _ = StoreProfile.objects.get_or_create(user=user)
    StoreProfile.objects.filter(pk=profile.pk).update(login_count=F('login_count') + 1)


@receiver(user_logged_out)
def release_device_login(sender, request, user, **kwargs):
    if request is None or user is None:
        return
    forget_device(user, request.session.session_key)
