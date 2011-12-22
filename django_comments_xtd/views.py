from django.db import models
from django.conf import settings
from django.contrib.comments.signals import comment_was_posted
from django.contrib.sites.models import Site
from django.core.urlresolvers import reverse
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import redirect, render_to_response
from django.template import loader, Context, RequestContext
from django.utils.translation import ugettext_lazy as _
from django.core.mail import send_mail, EmailMultiAlternatives

from django_comments_xtd import signals, signed
from django_comments_xtd.models import XtdComment, TmpXtdComment


COMMENTS_XTD_SALT = getattr(settings, 'COMMENTS_XTD_SALT', "")

COMMENTS_XTD_CONFIRM_EMAIL = getattr(settings, 'COMMENTS_XTD_CONFIRM_EMAIL', 
                                     True)


def send_email_confirmation_request(comment, target, key, text_template="django_comments_xtd/email_confirmation_request.txt", html_template="django_comments_xtd/email_confirmation_request.html"):
    """Send email requesting comment confirmation"""
    subject = _("comment confirmation request")
    confirmation_url = reverse("comments-xtd-confirm", args=[key])
    
    # prepare text message
    text_message_template = loader.get_template(text_template)
    text_message_context = Context({ 'comment': comment, 
                                'content_object': target, 
                                'confirmation_url': confirmation_url,
                                'contact': settings.DEFAULT_FROM_EMAIL,
                                'site': Site.objects.get_current() })
    text_message = text_message_template.render(text_message_context)

    # prepare html message
    html_message_template = loader.get_template(html_template)
    html_message_context = Context({ 'comment': comment, 
                                'content_object': target, 
                                'confirmation_url': confirmation_url,
                                'contact': settings.DEFAULT_FROM_EMAIL,
                                'site': Site.objects.get_current() })
    html_message = html_message_template.render(html_message_context)

    # create message
    message = EmailMultiAlternatives(subject, text_message, 
                                     settings.DEFAULT_FROM_EMAIL,
                                     [ comment.user_email, ])
    message.attach_alternative(html_message, "text/html")
    message.send()


def _comment_exists(comment):
    """
    True if exists a XtdComment with same user_name, user_email and submit_date.
    """
    return (XtdComment.objects.filter(
            user_name=comment.user_name, 
            user_email=comment.user_email,
            submit_date=comment.submit_date).count() > 0)


def _create_comment(tmp_comment):
    """
    Creates a XtdComment from a TmpXtdComment.
    """
    comment = XtdComment(**tmp_comment)
    comment.is_public = True
    comment.save()
    return comment


def on_comment_was_posted(sender, comment, request, **kwargs):
    """
    Post the comment if a user is authenticated or send a confirmation email.
    
    On signal django.contrib.comments.signals.comment_was_posted check if the 
    user is authenticated or if settings.COMMENTS_XTD_CONFIRM_EMAIL is False. 
    In both cases will post the comment. Otherwise will send a confirmation
    email to the person who posted the comment.
    """
    if ((comment.user and comment.user.is_authenticated())
        or not COMMENTS_XTD_CONFIRM_EMAIL):
        if not _comment_exists(comment):
            new_comment = _create_comment(comment)
            comment.xtd_comment = new_comment
            notify_comment_followers(new_comment)            
    else:
        ctype = request.POST["content_type"]
        object_pk = request.POST["object_pk"]
        model = models.get_model(*ctype.split("."))
        target = model._default_manager.get(pk=object_pk)
        key = signed.dumps(comment, compress=True, extra_key=COMMENTS_XTD_SALT)
        send_email_confirmation_request(comment, target, key)

comment_was_posted.connect(on_comment_was_posted)
    

def confirmation_requested(request, template="django_comments_xtd/confirmation_requested.html"):
    comment = None
    if request.GET.get("c", "") != "":
        try:
            comment = XtdComment.objects.get(pk=request.GET['c'])
        except (XtdComment.DoesNotExist, ValueError):
            pass

    if comment:
        return HttpResponseRedirect(comment.get_absolute_url())
    else:
        return render_to_response(
            template, context_instance=RequestContext(request))


def confirm(request, key, template_discarded="django_comments_xtd/discarded.html"):
    try:
        tmp_comment = signed.loads(key, extra_key=COMMENTS_XTD_SALT)
    except (ValueError, signed.BadSignature):
        raise Http404

    # the comment does exist if the URL was already confirmed, then: Http404
    if _comment_exists(tmp_comment):
        raise Http404

    # Send signal that the comment confirmation has been received
    responses = signals.confirmation_received.send(sender  = TmpXtdComment,
                                                   comment = tmp_comment,
                                                   request = request
    )

    # Check whether a signal receiver decides to discard the contact_msg
    for (receiver, response) in responses:
        if response == False:
            return render_to_response(template_discarded, 
                                      {'comment': tmp_comment},
                                      context_instance=RequestContext(request))

    comment = _create_comment(tmp_comment)
    notify_comment_followers(comment)
    return redirect(comment)


def notify_comment_followers(comment):
    followers = {}

    previous_comments = XtdComment.objects.filter(
        object_pk=comment.object_pk, is_public=True, 
        followup=True).exclude(id__exact=comment.id)

    for instance in previous_comments:
        followers[instance.user_email] = instance.user_name

    model = models.get_model(comment.content_type.app_label,
                             comment.content_type.model)
    target = model._default_manager.get(pk=comment.object_pk)
    subject = _("new comment posted")
    message_template = loader.get_template(
        "django_comments_xtd/email_followup_comment.txt")

    for email, name in followers.iteritems():
        message_context = Context({ 'user_name': name,
                                    'comment': comment, 
                                    'content_object': target, 
                                    'site': Site.objects.get_current() })
        message = message_template.render(message_context)
        send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, 
                  [ email, ])
    