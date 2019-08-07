import peewee

from .connections import database
from . import cache


class SerializerModelObjectCursorWrapper(peewee.ModelObjectCursorWrapper):
    def process_row(self, row):
        instance = super().process_row(row)
        if hasattr(self, '_serializer'):
            instance = self._serializer(instance)
        return instance


class SerializerModelCursorWrapper(peewee.ModelCursorWrapper):
    def process_row(self, row):
        instance = super().process_row(row)
        if hasattr(self, '_serializer'):
            instance = self._serializer(instance)
        return instance


class ModelSelect(peewee.ModelSelect):

    @peewee.database_required
    def execute(self, database):
        wrapper = super()._execute(database)
        if hasattr(self, '_serializer'):
            wrapper._serializer = self._serializer
        return wrapper

    @peewee.Node.copy
    def serialize(self, mask=None):
        self._serializer = lambda inst: inst.serialize(mask)
        if len(self._from_list) == 1 and not self._joins:
            self._result_wrapper = SerializerModelObjectCursorWrapper
        else:
            self._result_wrapper = SerializerModelCursorWrapper

    def _get_model_cursor_wrapper(self, cursor):
        wrapper = getattr(self, '_result_wrapper', None)
        if wrapper == SerializerModelObjectCursorWrapper:
            return wrapper(cursor, self.model, self._returning, self.model)
        elif wrapper == SerializerModelCursorWrapper:
            return wrapper(cursor, self.model, self._returning, self._from_list, self._joins)
        else:
            return super()._get_model_cursor_wrapper(cursor)


class Model(peewee.Model):

    # id is reserved for BAN external id, but lets be consistent and use the
    # same primary key name all over the models.
    pk = peewee.AutoField()

    class Meta:
        database = database
        manager = ModelSelect


    @classmethod
    def get_not_nullable_foreign_key_fields(cls):
        fields = cls._meta.sorted_fields
        foreign_key_fields = {}
        for f in fields:
            if isinstance(f, peewee.ForeignKeyField) and not f.null:
                foreign_key_fields[f.name] = f.rel_model
        return foreign_key_fields

    @classmethod
    def get_fk_need_alias_fields(cls):
        fields = cls._meta.sorted_fields
        fw = {}
        need_alias = []
        for f in fields:
            if isinstance(f, peewee.ForeignKeyField):
                if f.rel_model in fw:
                    need_alias.append(f.name)
                    need_alias.append(fw[f.rel_model])
                elif f.rel_model == cls:
                    need_alias.append(f.name)
                else:
                    fw[f.rel_model] = f.name
        return need_alias


    def save(self, *args, **kwargs):
        cache.clear()
        super().save(*args, **kwargs)

    @classmethod
    def select(cls, *fields):
        is_default = not fields
        if not fields:
            fields = cls._meta.sorted_fields
        query = cls._meta.manager(cls, fields, is_default=is_default)
        if hasattr(cls._meta, 'order_by'):
            order_by_list = [getattr(cls, o) for o in cls._meta.order_by]
            query = query.order_by(order_by_list)
        return query

    @classmethod
    def where(cls, *expressions):
        """Shortcut for select().where()"""
        return cls.select().where(*expressions)

    @classmethod
    def first(cls, *expressions):
        """Shortcut for select().where().first()"""
        qs = cls.select()
        if expressions:
            qs = qs.where(*expressions)
        # See https://github.com/coleifer/peewee/commit/eeb6d4d727da8536906a00c490f94352465e90bb  # noqa
        return qs.limit(1).first()

    def __setattr__(self, name, value):
        attr = getattr(self.__class__, name, None)
        if attr \
                and hasattr(attr, 'adapt') and not isinstance(attr, peewee.ForeignKeyField):
            # not nullable ForeignKeyFields are fetched by join
            value = attr.adapt(value)
        return super().__setattr__(name, value)
