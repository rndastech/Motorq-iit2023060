"""
Django settings for Motorq Vehicle Enrollment project.
"""

import os
from pathlib import Path

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', 'django-insecure-dev-key-change-in-production')

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = os.environ.get('DJANGO_DEBUG', 'True').lower() == 'true'

ALLOWED_HOSTS = os.environ.get('DJANGO_ALLOWED_HOSTS', '*').split(',')

# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'enrollment',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

# Database - SQLite default
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

# Internationalization
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

# Static files (CSS, JavaScript, Images)
STATIC_URL = 'static/'

# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# REST Framework settings
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'enrollment.authentication.APIKeyAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'enrollment.permissions.HasAPIKey',
    ],
    'UNAUTHENTICATED_TOKEN': None,
}

# API Key for authentication
API_KEY = os.environ.get('MOTORQ_API_KEY', 'dev-api-key-change-in-production')

# OEM API Configuration (base URLs per brand)
# Note: Don't include /api suffix here - endpoints already have it
OEM_API_BASE_URL = {
    'maruti': os.environ.get('MARUTI_API_URL', 'http://localhost:8001'),
    'toyota': os.environ.get('TOYOTA_API_URL', 'http://localhost:8002'),
    'tata': os.environ.get('TATA_API_URL', 'http://localhost:8003'),
}

# Async polling settings (deprecated, use pubsub instead)
ASYNC_POLL_INTERVAL_SECONDS = int(os.environ.get('ASYNC_POLL_INTERVAL', '10'))
ASYNC_MAX_RETRIES = int(os.environ.get('ASYNC_MAX_RETRIES', '3'))

# Redis configuration for pub/sub
REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
USE_FAKE_REDIS = os.environ.get('USE_FAKE_REDIS', 'false').lower() == 'true'  # Use fakeredis for testing

# Pub/sub settings
PUBSUB_CHANNEL_PREFIX = 'enrollment'
ENROLLMENT_SUCCESS_CODE = '6700'  # Success error code for Maruti enrollment