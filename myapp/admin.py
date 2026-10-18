from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.models import User

from .models import ContactSubmission, StoreProfile, Order, OrderItem, PaymentSettings, Payment, DropboxSettings, PhoneVerification, PWASettings, EmailSettings, EmailVerification, AIConversation, AIMessage, GitHubConnection, KnowledgeEntry, AIReport














@admin.register(PaymentSettings)
class PaymentSettingsAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'is_razorpay_enabled', 'is_test_mode', 'cod_enabled', 'updated_at')

    def has_add_permission(self, request):
        return not PaymentSettings.objects.exists()


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'order', 'customer_email', 'method', 'status', 'amount',
        'razorpay_order_id', 'razorpay_payment_id', 'created_at',
    )
    list_filter = ('method', 'status', 'created_at')
    search_fields = ('order__id', 'order__user__username', 'order__user__email', 'razorpay_order_id', 'razorpay_payment_id')
    readonly_fields = ('created_at', 'updated_at')
    date_hierarchy = 'created_at'

    @admin.display(description='Customer email', ordering='order__user__email')
    def customer_email(self, obj):
        return obj.order.user.email


@admin.register(DropboxSettings)
class DropboxSettingsAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'is_configured', 'updated_at')

    def has_add_permission(self, request):
        return not DropboxSettings.objects.exists()




@admin.register(PWASettings)
class PWASettingsAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'is_enabled', 'app_name', 'updated_at')

    def has_add_permission(self, request):
        return not PWASettings.objects.exists()




@admin.register(EmailSettings)
class EmailSettingsAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'is_enabled', 'smtp_host', 'smtp_username', 'ready', 'updated_at')

    def has_add_permission(self, request):
        return not EmailSettings.objects.exists()


@admin.register(EmailVerification)
class EmailVerificationAdmin(admin.ModelAdmin):
    list_display = ('user', 'attempts', 'created_at', 'expires_at')
    search_fields = ('user__username', 'user__email')


@admin.register(StoreProfile)
class StoreProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'phone', 'manual_amount_paid', 'manual_payment_received_at', 'wallet_balance', 'phone_verified', 'ai_display_name', 'ai_location', 'ai_instagram_handle')
    search_fields = ('user__username', 'user__email', 'phone', 'ai_display_name', 'ai_location', 'ai_instagram_handle')
    list_filter = ('phone_verified', 'ai_onboarded', 'manual_payment_received_at')


class AIMessageInline(admin.TabularInline):
    model = AIMessage
    extra = 0
    readonly_fields = ('role', 'content', 'image_data', 'document_name', 'model_key', 'created_at')
    can_delete = False


@admin.register(AIConversation)
class AIConversationAdmin(admin.ModelAdmin):
    # who_asked makes it obvious at a glance whether a conversation belongs
    # to a real account or an anonymous guest, without opening each row.
    list_display = ('__str__', 'who_asked', 'created_at', 'updated_at')
    search_fields = ('title', 'user__username', 'user__email', 'session_key')
    list_filter = ('created_at',)
    inlines = [AIMessageInline]

    def who_asked(self, obj):
        if obj.user:
            return obj.user.get_full_name() or obj.user.username
        return f'Guest ({obj.session_key[:10]}…)' if obj.session_key else 'Guest'
    who_asked.short_description = 'Asked by'


@admin.register(AIMessage)
class AIMessageAdmin(admin.ModelAdmin):
    list_display = ('conversation', 'who_asked', 'role', 'attachment', 'model_key', 'created_at')
    list_select_related = ('conversation', 'conversation__user')
    list_filter = ('role', 'model_key')
    search_fields = ('content', 'document_name', 'conversation__title', 'conversation__user__username', 'conversation__user__email', 'conversation__session_key')

    def who_asked(self, obj):
        user = obj.conversation.user
        if user:
            return user.get_full_name() or user.username
        session_key = obj.conversation.session_key
        return f'Guest ({session_key[:10]}…)' if session_key else 'Guest'
    who_asked.short_description = 'Asked by'

    def attachment(self, obj):
        if obj.document_name:
            return f'📄 {obj.document_name}'
        if obj.image_data:
            return '🖼️ image'
        return ''
    attachment.short_description = 'Attachment'


@admin.register(AIReport)
class AIReportAdmin(admin.ModelAdmin):
    # reported_by mirrors AIConversationAdmin.who_asked — shows at a glance
    # whether the report came from a real account (with the email needed to
    # follow up) or an anonymous guest, without opening each row.
    list_display = ('id', 'reported_by', 'issue_summary', 'model_key', 'status', 'created_at')
    list_editable = ('status',)
    list_filter = ('status', 'model_key', 'created_at')
    search_fields = (
        'explanation', 'reported_reply', 'user_prompt', 'user_document_name', 'user__username',
        'user__email', 'session_key', 'conversation__title',
    )
    list_select_related = ('user', 'conversation')
    readonly_fields = (
        'conversation', 'message', 'user_prompt', 'user_image',
        'user_document_name', 'user_document_excerpt',
        'reported_reply', 'reported_image', 'model_key',
        'user', 'session_key', 'ip_address', 'created_at',
    )

    def reported_by(self, obj):
        if obj.user:
            return f"{obj.user.get_full_name() or obj.user.username} ({obj.user.email or 'no email'})"
        return f'Guest ({obj.session_key[:10]}…)' if obj.session_key else 'Guest'
    reported_by.short_description = 'Reported by'

    def issue_summary(self, obj):
        text = obj.explanation.strip()
        return (text[:60] + '…') if len(text) > 60 else (text or '(no explanation given)')
    issue_summary.short_description = 'Issue'


@admin.register(KnowledgeEntry)
class KnowledgeEntryAdmin(admin.ModelAdmin):
    # Legacy data from the removed EduTrellis Light feature — see
    # KnowledgeEntry's docstring. Kept browsable here only; no AI reply
    # reads from this table any more. visible_to makes it obvious whether an
    # entry was shared with everyone or private to one person.
    list_display = ('topic', 'source', 'visible_to', 'updated_at')
    list_select_related = ('user',)
    list_filter = ('source',)
    search_fields = ('topic', 'content', 'user__username', 'user__email', 'session_key')

    def visible_to(self, obj):
        if obj.user:
            return obj.user.get_full_name() or obj.user.username
        if obj.session_key:
            return f'Guest ({obj.session_key[:10]}…)'
        return 'Everyone'
    visible_to.short_description = 'Visible to'


@admin.register(GitHubConnection)
class GitHubConnectionAdmin(admin.ModelAdmin):
    # access_token deliberately left out of list_display so it's never
    # casually visible while scanning the list view.
    list_display = ('user', 'github_username', 'repo_full_name', 'default_branch', 'connected_at')
    search_fields = ('user__username', 'github_username', 'repo_full_name')


@admin.register(PhoneVerification)
class PhoneVerificationAdmin(admin.ModelAdmin):
    list_display = ('user', 'phone', 'attempts', 'created_at', 'expires_at')
    search_fields = ('user__username', 'user__email', 'phone')








class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    readonly_fields = ('product_id', 'product_name', 'price', 'quantity')
    can_delete = False


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'status', 'total', 'wallet_credit_applied', 'created_at')
    list_filter = ('status', 'wallet_credit_applied')
    search_fields = ('user__username', 'user__email')
    inlines = [OrderItemInline]

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        obj.maybe_credit_wallet()


class StoreProfileInline(admin.StackedInline):
    model = StoreProfile
    can_delete = False
    verbose_name_plural = 'Store Profile'


class StoreUserAdmin(UserAdmin):
    inlines = [StoreProfileInline]
    list_display = UserAdmin.list_display + ('store_phone',)

    def store_phone(self, obj):
        return getattr(obj.store_profile, 'phone', '')
    store_phone.short_description = 'Phone'


admin.site.unregister(User)
admin.site.register(User, StoreUserAdmin)


@admin.register(ContactSubmission)
class ContactSubmissionAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'source', 'name', 'email', 'phone', 'service')
    list_filter = ('source', 'service', 'created_at')
    search_fields = ('name', 'email', 'phone', 'message')
    readonly_fields = ('created_at', 'ip_address')
