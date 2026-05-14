from django.contrib import admin
from .models import Package,GlobalSizePackage,User

admin.site.register(Package)
admin.site.register(GlobalSizePackage)
admin.site.register(User)
# Register your models here.
