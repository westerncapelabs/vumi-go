# -*- coding: utf-8 -*-
# -*- test-case-name: go.api.go_api.tests.test_go_api -*-

"""JSON RPC API for Vumi Go front-end and others."""

import itertools

from twisted.application.internet import StreamServerEndpointService
from twisted.internet.defer import inlineCallbacks, DeferredList

from txjsonrpc.jsonrpc import addIntrospection
from txjsonrpc.jsonrpclib import Fault
from txjsonrpc.web.jsonrpc import JSONRPC

from vumi.config import ConfigDict, ConfigText, ConfigServerEndpoint
from vumi.rpc import signature, Unicode, List, Dict
from vumi.transports.httprpc import httprpc
from vumi.utils import build_web_site
from vumi.worker import BaseWorker

from go.api.go_api.api_types import (
    CampaignType, ConversationType, ChannelType, RouterType,
    RoutingEntryType, RoutingType)
from go.api.go_api.auth import GoUserRealm, GoUserAuthSessionWrapper
from go.base.utils import get_conversation_definition, get_router_definition
from go.vumitools.account import RoutingTableHelper
from go.vumitools.api import VumiApi


class InvalidRoutingTable(Fault):
    """Raised when a routing table contains invalid endpoints."""
    FAULT_CODE = 400

    def __init__(self, msg):
        super(InvalidRoutingTable, self).__init__(self.FAULT_CODE, msg)


class GoApiServer(JSONRPC):
    def __init__(self, user_account_key, vumi_api):
        JSONRPC.__init__(self)
        self.user_account_key = user_account_key
        self.vumi_api = vumi_api

    def _conversations(self, user_api):
        def format_conversations(convs):
            return [ConversationType.format_conversation(c) for c in convs]

        d = user_api.active_conversations()
        d.addCallback(format_conversations)
        return d

    def _channels(self, user_api):
        def endpoints_to_channels(endpoints):
            return [ChannelType.format_channel(tag) for tag in endpoints]

        d = user_api.list_endpoints()
        d.addCallback(endpoints_to_channels)
        return d

    def _routers(self, user_api):
        def format_routers(routers):
            return [RouterType.format_router(rb)
                    for rb in routers]

        d = user_api.active_routers()
        d.addCallback(format_routers)
        return d

    def _routing_entries(self, user_api):
        def format_routing_entries(routing_table):
            routing_table = RoutingTableHelper(routing_table)
            return [RoutingEntryType.format_entry(
                source_uuid=u"%s:%s" % (src_conn, src_endp),
                target_uuid=u"%s:%s" % (dst_conn, dst_endp))
                for src_conn, src_endp, dst_conn, dst_endp
                in routing_table.entries()]

        d = user_api.get_routing_table()
        d.addCallback(format_routing_entries)
        return d

    @signature(returns=List("List of campaigns.",
                            item_type=CampaignType()))
    def jsonrpc_campaigns(self):
        """List the campaigns a user has access to."""
        return [CampaignType.format_campaign({
            'key': self.user_account_key,
            'name': u"Your Campaign",
        })]

    @signature(campaign_key=Unicode("Campaign key."),
               returns=List("List of conversations.",
                            item_type=ConversationType()))
    def jsonrpc_conversations(self, campaign_key):
        """List the active conversations under a particular campaign.
           """
        user_api = self.vumi_api.get_user_api(campaign_key)
        return self._conversations(user_api)

    @signature(campaign_key=Unicode("Campaign key."),
               returns=List("List of channels.",
                            item_type=ChannelType()))
    def jsonrpc_channels(self, campaign_key):
        """List the active channels under a particular campaign.
           """
        user_api = self.vumi_api.get_user_api(campaign_key)
        return self._channels(user_api)

    @signature(campaign_key=Unicode("Campaign key."),
               returns=List("List of routers.",
                            item_type=RouterType()))
    def jsonrpc_routers(self, campaign_key):
        """List the active routers under a particular campaign.
           """
        user_api = self.vumi_api.get_user_api(campaign_key)
        return self._routers(user_api)

    @signature(campaign_key=Unicode("Campaign key."),
               returns=List("List of routing table entries.",
                            item_type=RoutingEntryType()))
    def jsonrpc_routing_entries(self, campaign_key):
        """List the routing entries from a particular campaign's routing table.
           """
        user_api = self.vumi_api.get_user_api(campaign_key)
        return self._routing_entries(user_api)

    @signature(campaign_key=Unicode("Campaign key."),
               returns=RoutingType(
                   "Complete description of the routing table."))
    def jsonrpc_routing_table(self, campaign_key):
        """List the channels, conversations, routers and routing table
        entries that make up a campaign's routing.
        """
        user_api = self.vumi_api.get_user_api(campaign_key)
        deferreds = []
        deferreds.append(self._channels(user_api))
        deferreds.append(self._routers(user_api))
        deferreds.append(self._conversations(user_api))
        deferreds.append(self._routing_entries(user_api))

        def construct_json(results):
            for success, result in results:
                if not success:
                    result.raiseException()
            results = [r[1] for r in results]
            channels, routers, conversations, routing_entries = results
            return RoutingType.format_routing(
                channels, routers, conversations, routing_entries)

        d = DeferredList(deferreds, consumeErrors=True)
        d.addCallback(construct_json)
        return d

    @signature(campaign_key=Unicode("Campaign key."),
               routing=RoutingType("Description of the new routing table."))
    def jsonrpc_update_routing_table(self, campaign_key, routing):
        user_api = self.vumi_api.get_user_api(campaign_key)
        deferreds = []
        deferreds.append(self._channels(user_api))
        deferreds.append(self._routers(user_api))
        deferreds.append(self._conversations(user_api))

        def gather_endpoints(results):
            for success, result in results:
                if not success:
                    result.raiseException()
            results = [r[1] for r in results]
            channels, routers, conversations = results

            recv_outbound_endpoints = set(
                endpoint['uuid'] for endpoint in itertools.chain(
                    (e for c in channels for e in c['endpoints']),
                    (e for r in routers
                     for e in r['conversation_endpoints']),
                )
            )
            recv_inbound_endpoints = set(
                endpoint['uuid'] for endpoint in itertools.chain(
                    (e for c in conversations for e in c['endpoints']),
                    (e for r in routers
                     for e in r['channel_endpoints'])
                )
            )

            return recv_outbound_endpoints, recv_inbound_endpoints

        def check_routing_table(endpoint_sets):
            """Check that endpoints link from known receives-outbound (right)
            endpoints to known receives-inbound (left) endpoints or vice
            versa.
            """
            recv_outbound_endpoints, recv_inbound_endpoints = endpoint_sets
            routing_entries = routing['routing_entries']
            for entry in routing_entries:
                source, target = entry['source'], entry['target']
                src_uuid, dst_uuid = source['uuid'], target['uuid']
                if src_uuid in recv_outbound_endpoints:
                    if dst_uuid not in recv_inbound_endpoints:
                        raise InvalidRoutingTable(
                            "Source outbound-receiving endpoint %r should"
                            " link to an inbound-receiving endpoint but links"
                            " to %r" % (source, target))
                elif src_uuid in recv_inbound_endpoints:
                    if dst_uuid not in recv_outbound_endpoints:
                        raise InvalidRoutingTable(
                            "Source inbound-receiving endpoint %r should"
                            " link to an outbound-receiving endpoint but links"
                            " to %r" % (source, target))
                else:
                    raise InvalidRoutingTable("Unknown source endpoint %r"
                                              % (source,))
            return routing_entries

        def populate_routing_table(routing_entries):
            routing_table = {}
            rt_helper = RoutingTableHelper(routing_table)
            for entry in routing_entries:
                source, target = entry['source'], entry['target']
                src_conn, _, src_endp = source['uuid'].rpartition(":")
                dst_conn, _, dst_endp = target['uuid'].rpartition(":")
                rt_helper.add_entry(src_conn, src_endp, dst_conn, dst_endp)

            d = user_api.get_user_account()
            d.addCallback(save_routing_table, routing_table)
            return d

        def save_routing_table(user_account, routing_table):
            user_account.routing_table = routing_table
            return user_account.save()

        def swallow_result(result):
            return None

        d = DeferredList(deferreds, consumeErrors=True)
        d.addCallback(gather_endpoints)
        d.addCallback(check_routing_table)
        d.addCallback(populate_routing_table)
        d.addCallback(swallow_result)
        return d

    @signature(campaign_key=Unicode("Campaign key."),
               conversation_key=Unicode("Conversation key."),
               action=Unicode("Name of action."),
               params=Dict("Additional action paramaters.", null=True),
               returns=Dict("Result of the action.", null=True))
    def jsonrpc_conversation_action(self, campaign_key, conversation_key,
                                    action, params):
        user_api = self.vumi_api.get_user_api(campaign_key)
        d = user_api.get_conversation(conversation_key)

        def conversation_action(conv):
            conv_def = get_conversation_definition(
                conv.conversation_type, conv)
            return conv_def.api_action(action, params)

        d.addCallback(conversation_action)
        return d

    @signature(campaign_key=Unicode("Campaign key."),
               router_key=Unicode("Router key."),
               action=Unicode("Name of action."),
               params=Dict("Additional action paramaters.", null=True),
               returns=Dict("Result of the action.", null=True))
    def jsonrpc_router_action(self, campaign_key, router_key,
                              action, params):
        user_api = self.vumi_api.get_user_api(campaign_key)
        d = user_api.get_router(router_key)

        def router_action(router):
            router_def = get_router_definition(
                router.router_type, router)
            return router_def.api_action(action, params)

        d.addCallback(router_action)
        return d


class GoApiWorker(BaseWorker):

    class CONFIG_CLASS(BaseWorker.CONFIG_CLASS):
        worker_name = ConfigText(
            "Name of this Go API worker.", required=True, static=True)
        twisted_endpoint = ConfigServerEndpoint(
            "Twisted endpoint to listen on.", required=True, static=True)
        web_path = ConfigText(
            "The path to serve this resource on.", required=True, static=True)
        health_path = ConfigText(
            "The path to server the health resource on.", default='/health/',
            static=True)
        redis_manager = ConfigDict(
            "Redis client configuration.", default={}, static=True)
        riak_manager = ConfigDict(
            "Riak client configuration.", default={}, static=True)

    _web_service = None

    def _rpc_resource_for_user(self, username):
        rpc = GoApiServer(username, self.vumi_api)
        addIntrospection(rpc)
        return rpc

    def get_health_response(self):
        return "OK"

    @inlineCallbacks
    def setup_worker(self):
        config = self.get_static_config()
        self.vumi_api = yield VumiApi.from_config_async({
            'redis_manager': config.redis_manager,
            'riak_manager': config.riak_manager,
        })
        self.realm = GoUserRealm(self._rpc_resource_for_user)
        site = build_web_site({
            config.web_path: GoUserAuthSessionWrapper(
                self.realm, self.vumi_api),
            config.health_path: httprpc.HttpRpcHealthResource(self),
        })
        self._web_service = StreamServerEndpointService(
            config.twisted_endpoint, site)
        self._web_service.startService()

    @inlineCallbacks
    def teardown_worker(self):
        if self._web_service is not None:
            yield self._web_service.stopService()

    def setup_connectors(self):
        pass
