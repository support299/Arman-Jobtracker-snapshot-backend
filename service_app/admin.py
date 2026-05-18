from django.contrib import admin
from .models import Package,GlobalSizePackage,User,GlobalPackageTemplate,ServicePackageSizeMapping
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin


admin.site.register(Package)
admin.site.register(GlobalSizePackage)
admin.site.register(GlobalPackageTemplate)
admin.site.register(ServicePackageSizeMapping)

@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    """Django admin for custom User (GHL account, role, app flags)."""

    list_display = (
        'username',
        'email',
        'first_name',
        'last_name',
        'role',
        'is_admin',
        'account',
        'is_staff',
        'is_active',
    )
    list_filter = (
        'is_staff',
        'is_superuser',
        'is_active',
        'role',
        'account',
    )
    search_fields = (
        'username',
        'first_name',
        'last_name',
        'email',
        'ghl_user_id',
    )
    ordering = ('username',)
    raw_id_fields = ('account',)
    readonly_fields = (
        'is_admin',
        'created_at',
        'updated_at',
        'last_login',
        'date_joined',
    )

    fieldsets = (
        (None, {'fields': ('username', 'password')}),
        ('Personal info', {'fields': ('first_name', 'last_name', 'email')}),
        (
            'GHL & account',
            {
                'fields': ('account', 'ghl_user_id', 'role', 'is_admin'),
                'description': (
                    'is_admin is set automatically on save from role '
                    '(manager and supervisor are admins).'
                ),
            },
        ),
        (
            'App access',
            {
                'fields': (
                    'payroll_can_view_team_data',
                    'can_access_service_management_tool',
                    'can_access_location_management_tool',
                    'can_access_house_size_management_tool',
                ),
            },
        ),
        (
            'Permissions',
            {
                'fields': (
                    'is_active',
                    'is_staff',
                    'is_superuser',
                    'groups',
                    'user_permissions',
                ),
            },
        ),
        (
            'Important dates',
            {
                'fields': (
                    'last_login',
                    'date_joined',
                    'created_at',
                    'updated_at',
                ),
            },
        ),
    )

    add_fieldsets = (
        (
            None,
            {
                'classes': ('wide',),
                'fields': (
                    'username',
                    'password1',
                    'password2',
                    'email',
                    'first_name',
                    'last_name',
                    'role',
                    'account',
                    'is_staff',
                    'is_superuser',
                ),
            },
        ),
    )



