from datetime import datetime
import uuid

import peewee
from postgis import Point

from ban import db
from ban.utils import utcnow



from .exceptions import (IsDeletedError, MultipleRedirectsError, RedirectError,
                         ResourceLinkedError)
from .validators import ResourceValidator


class BaseResource(peewee.ModelBase):

    def include_field_for_collection(cls, name):
        if name in cls.exclude_for_collection:
            return False
        attr = getattr(cls, name, None)

        exclude = (peewee.SelectQuery,)
        if not attr or isinstance(attr, exclude):
            return False
        return True

    def __new__(mcs, name, bases, attrs, **kwargs):
        # Inherit and extend instead of replacing.
        resource_fields = attrs.pop('resource_fields', None)
        exclude_for_collection = attrs.pop('exclude_for_collection', None)
        exclude_for_version = attrs.pop('exclude_for_version', None)
        cls = super().__new__(mcs, name, bases, attrs, **kwargs)
        if resource_fields is not None:
            inherited = getattr(cls, 'resource_fields', {})
            resource_fields.extend(inherited)
            cls.resource_fields = resource_fields
        if exclude_for_collection is not None:
            inherited = getattr(cls, 'exclude_for_collection', [])
            exclude_for_collection.extend(inherited)
            cls.exclude_for_collection = exclude_for_collection
        if exclude_for_version is not None:
            inherited = getattr(cls, 'exclude_for_version', [])
            exclude_for_version.extend(inherited)
            cls.exclude_for_version = exclude_for_version
        cls.collection_fields = [
            n for n in cls.resource_fields
            if mcs.include_field_for_collection(cls, n)] + ['resource']
        cls.versioned_fields = [
            n for n in cls.resource_fields
            if n not in cls.exclude_for_version]
        return cls


class ResourceModel(db.Model, metaclass=BaseResource):
    resource_fields = ['id', 'status']
    identifiers = []
    readonly_fields = ['id', 'pk', 'status', 'deleted_at']
    exclude_for_collection = ['status']
    exclude_for_version = []

    id = db.CharField(max_length=50, unique=True, null=False)
    deleted_at = db.DateTimeField(null=True, index=True)

    class Meta:
        validator = ResourceValidator

    @classmethod
    def make_id(cls):
        return 'ban-{}-{}'.format(cls.__name__.lower(), uuid.uuid4().hex)

    def save(self, *args, **kwargs):
        if not self.id:
            self.id = self.make_id()
        return super().save(*args, **kwargs)

    @classmethod
    def validator(cls, instance=None, update=False, **data):
        validator = cls._meta.validator(cls, update=update)
        validator.validate(data, instance=instance)
        return validator

    @property
    def resource(self):
        return self.__class__.__name__.lower()

    @property
    def serialized(self):
        return self.id

    def serialize(self, mask=None):
        if not mask:
            return self.serialized
        dest = {}
        for name, subfields in mask.items():
            if name == '*':
                return self.serialize({k: subfields
                                       for k in self.resource_fields})
            field = getattr(self.__class__, name, None)
            if not field:
                raise ValueError('Unknown field {}'.format(name))
            value = getattr(self, name)
            if value is not None:
                #@TODO-peewee-migration: verifier classe pour listes
                if isinstance(field, (db.ManyToManyField,
                                      peewee.BackrefAccessor)):
                    value = [v.serialize(subfields) for v in value]
                elif isinstance(field, db.ForeignKeyField):
                    value = value.serialize(subfields)
                elif isinstance(value, datetime):
                    value = value.isoformat()
                elif isinstance(value, Point):
                    value = value.geojson
            dest[name] = value
        return dest

    @property
    def as_resource(self):
        """Resource plus relations."""
        # All fields and all first level relations fields.
        return self.serialize({'*': {}})

    @property
    def as_version(self):
        """Resources plus relations references and metadata."""
        return self.serialize({f: {} for f in self.versioned_fields})

    @property
    def as_export(self):
        """Flat resources plus references. May be filtered or overrided."""
        return self.serialize({'*': {}})

    @property
    def status(self):
        return 'deleted' if self.deleted_at else 'active'

    @classmethod
    def select(cls, *selection):
        return super().select(*selection)

    @classmethod
    def select_eagerly(cls, *selection):
        #@TODO foreign key field null is not managed well with left outer join
        if not hasattr(cls, 'get_not_nullable_foreign_key_fields'):
            return super().select(*selection)
        need_alias = cls.get_fk_need_alias_fields()
        fk_fields = cls.get_not_nullable_foreign_key_fields()
        for name, field in fk_fields.items():
            if name in need_alias:
                fk_fields[name] = field.alias()
        selection = (cls,) + tuple(fk_fields.values())
        select = super().select(*selection)
        count_alias = 0
        for name, field in fk_fields.items():
            if name in need_alias:
                count_alias = count_alias + 1
                select = select.join(field, peewee.JOIN.LEFT_OUTER, on=(getattr(cls, name) == getattr(field, 'pk')).alias(name))
            else:
                select = select.join(field, peewee.JOIN.LEFT_OUTER)
            if fk_fields != need_alias.__len__():
                select = select.switch(cls)
        return select

    @classmethod
    def raw_select(cls, *selection):
        return super().select(*selection)

    def mark_deleted(self):
        if self.deleted_at:
            raise ValueError('Resource already marked as deleted')
        self.ensure_no_reverse_relation()
        self.deleted_at = utcnow()
        self.increment_version()
        self.save()

    def ensure_no_reverse_relation(self):
        for foreign_key in self._meta.backrefs:
            select = getattr(self, foreign_key.backref)
            if getattr(select.model,'deleted_at', None):
                select = select.where(select.model.deleted_at.is_null())
            if select.count():
                raise ResourceLinkedError(
                    'Resource still linked by `{}`'.format(foreign_key.backref))

    @classmethod
    def adapt(cls, id, identifier=None, level1=0):

        if isinstance(id, db.Model):
            instance = id
        else:
            if not identifier:
                identifier = 'id'  # BAN id by default.
                if isinstance(id, str):
                    *extra, id = id.split(':')
                    if extra:
                        identifier = extra[0]
                    if identifier not in cls.identifiers + ['id', 'pk']:
                        raise cls.DoesNotExist("Invalid identifier {}".format(
                                                                identifier))
                elif isinstance(id, int):
                    identifier = 'pk'
            try:
                if not hasattr(cls, 'auth') and level1 != 1 and identifier != cls._meta.model.pk.name:
                    instance = cls.raw_select(cls._meta.model.pk).where(
                        getattr(cls, identifier) == id).get()
                elif not hasattr(cls, 'auth') and level1 != 1 and identifier == cls._meta.model.pk.name:
                    instance = id
                elif level1 == 1:
                    instance = cls.select_eagerly().where(
                        getattr(cls, identifier) == id).get()
                else:
                    instance = cls.raw_select().where(
                        getattr(cls, identifier) == id).get()

            except cls.DoesNotExist:
                # Is it an old identifier?
                from .versioning import Redirect
                redirects = Redirect.follow(cls.__name__, identifier, id)
                if redirects:
                    if len(redirects) > 1:
                        raise MultipleRedirectsError(identifier, id, redirects)
                    raise RedirectError(identifier, id, redirects[0])
                raise
        return instance
