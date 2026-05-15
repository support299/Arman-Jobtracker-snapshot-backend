from django.contrib import admin

from .models import GHLLocationIndex, GHLAuthCredentials, Address, Contact, Calendar, GHLCompanyAuth



# Register your models here.
admin.site.register(GHLLocationIndex)
admin.site.register(GHLAuthCredentials)
admin.site.register(GHLCompanyAuth)
admin.site.register(Address)
admin.site.register(Contact)
admin.site.register(Calendar)