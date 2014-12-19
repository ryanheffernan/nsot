from __future__ import unicode_literals

from datetime import datetime
from operator import attrgetter
import functools
import ipaddress
import json
import logging

from sqlalchemy import create_engine, or_, union_all, desc
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import relationship, object_session, aliased
from sqlalchemy.orm import sessionmaker, Session as _Session
from sqlalchemy.schema import Column, ForeignKey, Index
from sqlalchemy.sql import func, label, literal
from sqlalchemy.types import Integer, String, Text, Boolean, SmallInteger
from sqlalchemy.types import Enum, DateTime, VARBINARY


class Session(_Session):
    """ Custom session meant to utilize add on the model.

        This Session overrides the add/add_all methods to prevent them
        from being used. This is to for using the add methods on the
        models themselves where overriding is available.
    """

    _add = _Session.add
    _add_all = _Session.add_all

    def add(self, *args, **kwargs):
        raise NotImplementedError("Use add method on models instead.")

    def add_all(self, *args, **kwargs):
        raise NotImplementedError("Use add method on models instead.")


Session = sessionmaker(class_=Session)


class Model(object):
    """ Custom model mixin with helper methods. """

    @property
    def session(self):
        return object_session(self)

    @classmethod
    def get_or_create(cls, session, **kwargs):
        instance = session.query(cls).filter_by(**kwargs).scalar()
        if instance:
            return instance, False

        instance = cls(**kwargs)
        instance.add(session)

        return instance, True

    def add(self, session):
        session._add(self)
        return self


Model = declarative_base(cls=Model)


def get_db_engine(url):
    return create_engine(url, pool_recycle=300)


def flush_transaction(method):
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        dryrun = kwargs.pop("dryrun", False)
        try:
            ret = method(self, *args, **kwargs)
            if dryrun:
                self.session.rollback()
            else:
                self.session.flush()
        except Exception:
            logging.exception("Transaction Failed. Rolling back.")
            if self.session is not None:
                self.session.rollback()
            raise
        return ret
    return wrapper


class Site(Model):
    """ A namespace for subnets, ipaddresses, attributes. """

    __tablename__ = "sites"

    id = Column(Integer, primary_key=True)
    name = Column(String(length=32), unique=True, nullable=False)
    description = Column(Text)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
        }


class Network(Model):
    """ Represents a subnet or ipaddress. """

    __tablename__ = "networks"
    __table_args__ = (
        Index(
            "cidr_idx",
            "site_id", "ip_version", "network_address", "prefix_length",
            unique=True
        ),
    )

    id = Column(Integer, primary_key=True)
    site_id = Column(Integer, ForeignKey("sites.id"), nullable=False, index=True)

    ip_version = Column(Enum("4", "6"), nullable=False, index=True)

    # Root networks will be NULL while other networks will point to
    # their supernet.
    parent_id = Column(Integer, ForeignKey("networks.id"), nullable=True)

    network_address = Column(VARBINARY(16), nullable=False, index=True)
    # While derivable from network/prefix this is useful as it enables
    # easy querys of the nested set variety.
    broadcast_address = Column(VARBINARY(16), nullable=False, index=True)

    prefix_length = Column(Integer, nullable=False, index=True)

    # Simple boolean
    is_ip = Column(Boolean, nullable=False, default=False, index=True)

    # Attributes is a Serialized LOB field. Lookups of these attributes
    # is done against an Inverted Index table
    _attributes = Column("attributes", Text, nullable=False)

    @property
    def attributes(self):
        return json.loads(self._attributes)

    @attributes.setter
    def attributes(self, attributes):
        if not isinstance(attributes, dict):
            raise TypeError("Expected dict.")

        for key, value in attributes.iteritems():
            if not isinstance(key, basestring):
                raise ValueError("Attribute keys must be a string type")
            if not isinstance(value, basestring):
                raise ValueError("Attribute values must be a string type")

        self._attributes = json.dumps(attributes)

    def supernets(self, session, direct=False, discover_mode=False, for_update=False):
        """ Get networks that are a supernet of a network.

            Args:
                direct: Only return direct supernet.
                discover_mode: Prevent new networks from bailing for missing parent_id
                for_update: Lock these rows because they're selected for updating.

        """

        if self.parent_id is None and not discover_mode:
            return []

        if discover_mode and direct:
            raise ValueError("direct is incompatible with discover_mode")

        query = session.query(Network)
        if for_update:
            query = query.with_for_update()

        if direct:
            return query.filter(Network.id == self.parent_id).all()

        return query.filter(
            Network.is_ip == False,
            Network.ip_version == self.ip_version,
            Network.prefix_length < self.prefix_length,
            Network.network_address <= self.network_address,
            Network.broadcast_address >= self.broadcast_address
        ).all()

    def subnets(self, session, include_networks=True, include_ips=False, direct=False, for_update=False):
        """ Get networks that are subnets of a network.

            Args:
                include_networks: Whether the response should include non-ip address networks
                include_ips: Whether the response should include ip addresses
                direct: Only return direct subnets.
                for_update: Lock these rows because they're selected for updating.
        """

        if not any([include_networks, include_ips]) or self.is_ip:
            return []

        query = session.query(Network)
        if for_update:
            query = query.with_for_update()

        if not all([include_networks, include_ips]):
            if include_networks:
                query = query.filter(Network.is_ip == False)
            if include_ips:
                query = query.filter(Network.is_ip == True)

        if direct:
            return query.filter(Network.parent_id == self.id).all()

        return query.filter(
            Network.ip_version == self.ip_version,
            Network.prefix_length > self.prefix_length,
            Network.network_address >= self.network_address,
            Network.broadcast_address <= self.broadcast_address
        ).all()

    @property
    def cidr(self):
        return "{}/{}".format(
            ipaddress.ip_address(self.network_address),
            self.prefix_length
        )

    def __repr__(self):
        return "Network<{}>".format(self.cidr)

    def reparent_subnets(self, session):
        query = session.query(Network).filter(
            Network.parent_id == self.parent_id,
            Network.id != self.id  # Don't include yourself...
        )

        # When adding a new root we're going to reparenting a subset
        # of roots so it's a bit more complicated so limit to all subnetworks
        if self.parent_id is None:
            query = query.filter(
                Network.is_ip == False,
                Network.ip_version == self.ip_version,
                Network.prefix_length > self.prefix_length,
                Network.network_address >= self.network_address,
                Network.broadcast_address <= self.broadcast_address
            )

        query.update({Network.parent_id: self.id})

    @classmethod
    def create(cls, session, site_id, cidr, attributes=None):
        if attributes is None:
            attributes = {}

        network = ipaddress.ip_network(cidr)

        is_ip = False
        if network.network_address == network.broadcast_address:
            is_ip = True

        kwargs = {
            "site_id": site_id,
            "ip_version": str(network.version),
            "network_address": network.network_address.packed,
            "broadcast_address": network.broadcast_address.packed,
            "prefix_length": network.prefixlen,
            "is_ip": is_ip,
            "attributes": attributes,
        }

        try:
            obj = cls(**kwargs)
            obj.add(session)
            # Need to get a primary key for the new network to update subnets.
            session.flush()

            supernets = obj.supernets(session, discover_mode=True, for_update=True)
            if supernets:
                parent = max(supernets, key=attrgetter("prefix_length"))
                obj.parent_id = parent.id

            obj.reparent_subnets(session)
            session.commit()
        except Exception:
            session.rollback()
            raise  # TODO(gary) Raise better exception

        return obj


class Hostname(Model):

    __tablename__ = "hostnames"

    id = Column(Integer, primary_key=True)
    network_id = Column(Integer, ForeignKey("networks.id"), nullable=False)
    # Not unique to allow for secondary round-robin names for an IP
    name = Column(String, nullable=False)
    # The primary hostname will be used for reverse DNS. Only one primary
    # hostname is allowed.
    primary = Column(Boolean, nullable=False, index=True)


class NetworkAttribute(Model):

    __tablename__ = "network_attributes"
    __table_args__ = (
        Index(
            "name_idx",
            "site_id", "name",
            unique=True
        ),
    )

    id = Column(Integer, primary_key=True)
    site_id = Column(Integer, ForeignKey("sites.id"), nullable=False)

    name = Column(String, nullable=False)

    required = Column(Boolean, default=False, nullable=False)
    cascade = Column(Boolean, default=True, nullable=False)

    def to_dict(self):
        return {
            "id": self.id,
            "site_id": self.site_id,
            "name": self.name,
            "required": self.required,
            "cascade": self.cascade,
        }


class NetworkAttributeIndex(Model):
    """ An Inverted Index for looking up Networks by their attributes."""

    __tablename__ = "network_attribute_index"
    __table_args__ = (
        # Ensure that each network can only have one of each attribute
        Index(
            "single_attr_idx",
            "network_id", "attribute_id",
            unique=True
        ),
    )

    id = Column(Integer, primary_key=True)

    name = Column(String, nullable=False, index=True)
    value = Column(String, nullable=False, index=True)

    network_id = Column(Integer, ForeignKey("networks.id"), nullable=False)
    attribute_id = Column(Integer, ForeignKey("network_attributes.id"), nullable=False)