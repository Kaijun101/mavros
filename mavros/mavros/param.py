# -*- coding: utf-8 -*-
# vim:set ts=4 sw=4 et:
#
# Copyright 2014,2021 Vladimir Ermakov.
#
# This file is part of the mavros package and subject to the license terms
# in the top-level LICENSE file of the mavros repository.
# https://github.com/mavlink/mavros/tree/master/LICENSE.md

import collections
import csv
import datetime
import typing

import rclpy
from mavros_msgs.msg import ParamEvent
from mavros_msgs.srv import ParamPull, ParamSetV2
from rcl_interfaces.msg import Parameter as ParameterMsg
from rcl_interfaces.msg import ParameterValue, SetParametersResult
from rcl_interfaces.srv import GetParameters, ListParameters, SetParameters
from rclpy.parameter import Parameter

from .base import (PluginModule, ServiceWaitTimeout, SubscriptionCallable,
                   cached_property)

TIMEOUT = 5.0


class ParamFile:
    """Base class for param file parsers"""

    parameters: typing.Optional[typing.Dict[str, Parameter]] = None
    stamp: typing.Optional[datetime.datetime] = None
    tgt_system = 1
    tgt_component = 1

    def load(self, file_: typing.TextIO):
        """Returns a iterable of Parameters"""
        raise NotImplementedError

    def save(self, file_: typing.TextIO):
        """Writes Parameters to file"""
        raise NotImplementedError


class MavProxyParam(ParamFile):
    """Parse MavProxy param files"""
    class CSVDialect(csv.Dialect):
        delimiter = ' '
        doublequote = False
        skipinitialspace = True
        lineterminator = '\r\n'
        quoting = csv.QUOTE_NONE

    def _parse_param_file(self, file_: typing.TextIO):
        to_numeric = lambda x: float(x) if '.' in x else int(x)

        for data in csv.reader(file_, self.CSVDialect):
            if data[0].startswith('#'):
                continue  # skip comments

            if len(data) != 2:
                raise ValueError("wrong field count")

            yield Parameter(data[0].strip(), value=to_numeric(data[1]))

    def load(self, file_: typing.TextIO):
        self.parameters = {p.name: p for p in self._parse_param_file(file_)}

    def save(self, file_: typing.TextIO):
        if self.stamp is None:
            self.stamp = datetime.datetime.now()

        writer = csv.writer(file_, self.CSVDialect)
        file_.writerow((f"""#NOTE: {self.stamp.strftime("%d.%m.%Y %T")}""", ))
        for p in self.parameters:
            writer.writerow((p.name, p.value))


class MissionPlannerParam(MavProxyParam):
    """Parse MissionPlanner param files"""
    class CSVDialect(csv.Dialect):
        delimiter = ','
        doublequote = False
        skipinitialspace = True
        lineterminator = '\r\n'
        quoting = csv.QUOTE_NONE


class QGroundControlParam(ParamFile):
    """Parse QGC param files"""
    class CSVDialect(csv.Dialect):
        delimiter = '\t'
        doublequote = False
        skipinitialspace = True
        lineterminator = '\n'
        quoting = csv.QUOTE_NONE

    def _parse_param_file(self, file_: typing.TextIO):
        to_numeric = lambda x: float(x) if '.' in x else int(x)

        for data in csv.reader(file_, self.CSVDialect):
            if data[0].startswith('#'):
                continue  # skip comments

            if len(data) != 5:
                raise ValueError("wrong field count")

            yield Parameter(data[2].strip(), value=to_numeric(data[3]))

    def load(self, file_: typing.TextIO):
        self.parameters = {p.name: p for p in self._parse_param_file(file_)}

    def save(self, file_: typing.TextIO):
        def to_type(x):
            if isinstance(x, float):
                return 9  # REAL32
            elif isinstance(x, int):
                return 6  # INT32
            else:
                raise ValueError(f"unknown type: {type(x):r}")

        if self.stamp is None:
            self.stamp = datetime.datetime.now()

        writer = csv.writer(file_, self.CSVDialect)
        writer.writerow(
            (f"""# NOTE: {self.stamp.strftime("%d.%m.%Y %T")}""", ))
        writer.writerow((
            f"# Onboard parameters saved by mavparam for ({self.tgt_system}.{self.tgt_component})",
        ))
        writer.writerow(
            ("# MAV ID", "COMPONENT ID", "PARAM NAME", "VALUE", "(TYPE)"))
        for p in self.parameters:
            writer.writerow((
                self.tgt_system,
                self.tgt_component,
                p.name,
                p.value,
                to_type(p.value),
            ))


class ParamPlugin(PluginModule):
    """
    Parameter plugin client
    """
    class ParamDict(collections.UserDict):

        _pm: 'ParamPlugin' = None

        def __getitem__(self, key: str) -> Parameter:
            return self.data[key]

        def __setitem__(self, key: str, value: Parameter):
            self.data[key] = value
            call_set_parameters(node=self._pm._node,
                                client=self._pm.set_parameters, [value])

        def __getattr__(self, key: str) -> Parameter:
            return self.data[key]

        def __setattr__(self, key: str, value: Parameter):
            self[key] = value

        def reset():
            self.data = {}

        def _event_handler(self, msg: ParamEvent):
            self.data[msg.param_id] = parameter_from_parameter_value(
                msg.param_id, msg.value)

    timeout_sec: float = 5.0
    _parameters = None
    _event_sub = None

    @cached_property
    def list_parameters(self) -> rclpy.node.Client:
        return self.create_client(ListParameters, ('param', 'list_parameters'))

    @cached_property
    def get_parameters(self) -> rclpy.node.Client:
        return self.create_client(GetParameters, ('param', 'get_parameters'))

    @cached_property
    def set_parameters(self) -> rclpy.node.Client:
        return self.create_client(SetParameters, ('param', 'set_parameters'))

    @cached_property
    def pull(self) -> rclpy.node.Client:
        return self.create_client(ParamPull, ('param', 'pull'))

    @cached_property
    def set(self) -> rclpy.node.Client:
        return self.create_client(ParamSetV2, ('param', 'set'))

    def subscribe_events(
        self,
        callback: SubscriptionCallable,
        qos_profile: rclpy.qos.QoSProfile = rclpy.qos.QoSPresetProfiles.
        PARAMETERS
    ) -> rclpy.node.Subscription:
        """
        Subscribe to parameter events
        """
        return self.create_subscription(ParamEvent, ('param', 'event'),
                                        callback, qos_profile)

    def call_pull(self, *, force_pull: bool = False) -> ParamPull.Response:
        lg = self._node.get_logger()

        ready = self.pull.wait_for_service(timeout_sec=self.timeout_sec)
        if not ready:
            raise ServiceWaitTimeout()

        req = ParamPull.Request()
        req.force_pull = force_pull

        future = self.pull.call_async(req)
        rclpy.spin_until_future_complete(self._node, future)

        resp = future.result()
        lg.debug(f"pull result: {resp}")

        return resp

    @property
    def param(self):
        if self._parameters is not None:
            return self._parameters

        self._parameters = self.ParamDict()
        self._parameters._pm = self

        self._event_sub = self.subscribe_events(
            self._parameters._event_handler)

        self.call_pull()
        return self._parameters


def call_list_parameters(self,
                         *,
                         node: rclpy.node.Node,
                         node_name: typing.Optional[str] = None,
                         client: typing.Optional[rclpy.node.Client] = None,
                         prefixes: typing.List[str] = []) -> typing.List[str]:
    lg = node.get_logger()

    if client is None:
        assert node_name is not None
        client = node.create_client(ListParameters,
                                    f"{node_name}/list_parameters")

    ready = client.wait_for_service(timeout_sec=TIMEOUT)
    if not ready:
        lg.error("wait for service time out")
        raise ServiceWaitTimeout()

    req = ListParameters.Request()
    req.prefixes = prefixes

    future = client.call_async(req)
    rclpy.spin_until_future_complete(node, future)

    resp = future.result()
    lg.debug(f"list result: {resp}")

    return resp.result.names


def call_get_parameters(
        self,
        *,
        node: rclpy.node.Node,
        node_name: typing.Optional[str] = None,
        client: typing.Optional[rclpy.node.Client] = None,
        names: typing.List[str] = []) -> typing.Dict[str, ParameterValue]:
    lg = node.get_logger()

    if client is None:
        assert node_name is not None
        client = node.create_client(GetParameters,
                                    f"{node_name}/get_parameters")

    ready = client.wait_for_service(timeout_sec=TIMEOUT)
    if not ready:
        lg.error("wait for service time out")
        raise ServiceWaitTimeout()

    req = GetParameters.Request()
    req.names = names

    future = client.call_async(req)
    rclpy.spin_until_future_complete(node, future)

    resp = future.result()
    lg.debug(f"get result: {resp}")

    return dict(zip(names, resp.values))


def call_set_parameters(
    self,
    *,
    node: rclpy.node.Node,
    node_name: typing.Optional[str] = None,
    client: typing.Optional[rclpy.node.Client] = None,
    parameters: typing.List[Parameter] = []
) -> typing.Dict[str, SetParametersResult]:
    lg = node.get_logger()

    if client is None:
        assert node_name is not None
        client = node.create_client(SetParameters,
                                    f"{node_name}/set_parameters")

    ready = client.wait_for_service(timeout_sec=TIMEOUT)
    if not ready:
        lg.error("wait for service time out")
        raise ServiceWaitTimeout()

    req = SetParameters.Request()
    req.parameters = [p.to_parameter_msg() for p in parameters]

    future = client.call_async(req)
    rclpy.spin_until_future_complete(node, future)

    resp = future.result()
    lg.debug(f"set result: {resp}")

    return dict(zip((p.name for p in parameters), resp.results))


def parameter_from_parameter_value(
        name: str, parameter_value: ParameterValue) -> Parameter:
    pmsg = ParameterMsg(name=name, value=parameter_value)
    return Parameter.from_parameter_msg(pmsg)
