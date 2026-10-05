"""Authenticated clan operations and stock PH packet projections."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from clan_db import ClanDBError, normalize_clan_name
from clan_wire import ClanWireError, decode, encode

SUCCESS = 0x9000
ROLES = {'captain': 1, 'vicecaptain': 2, 'instructor': 3, 'player': 4,
         'reserveplayer': 5, 'honorarycaptain': 6}
# The catalog's semantic types are logo/icon=0, frame=1, background=2.
# ClanPropInfo.Type references CommodityDisplayTypeEnums in proto_c2zn.tdr:
# BADGEICN=8, BADGEBKG=9, BADGEFRM=10. Catalog sale subtypes are a different
# enum. Sending 0/1/2 here cannot establish correct ownership in all tabs.
BADGE_PROP_WIRE_TYPE = {0: 8, 1: 10, 2: 9}
# IDs from the supplied PH proto_c2zn.tdr macro table. A601 is not aliased:
# the old creation guess conflicts with ID_C2ZN_REQ_CREATECLAN = B001.
COMMANDS = {
    0xB001: ('C2ZN_ReqCreateClan', 0xB002, 'ZN2C_ResCreateClan'),
    0xB005: ('C2ZN_ReqSearchClanByName', 0xB006, 'ZN2C_ResSearchClanByName'),
    0xB009: ('C2ZN_ReqClanDetail', 0xB00A, 'ZN2C_ResClanDetail'),
    0xB00B: ('C2ZN_ReqClanMembers', 0xB00C, 'ZN2C_ResClanMembers'),
    0xB00D: ('C2ZN_ReqClanCommon', 0xB00E, 'ZN2C_ResClanCommon'),
    0xB00F: ('C2ZN_ReqVerifyClanName', 0xB010, 'ZN2C_ResVerifyClanName'),
    0xB013: ('C2ZN_ReqJoinClan', 0xB014, 'ZN2C_ResJoinClan'),
    0xB015: ('C2ZN_ReqApproveList', 0xB016, 'ZN2C_ResApproveList'),
    0xB017: ('C2ZN_ReqConfirmJoin', 0xB018, 'ZN2C_ResConfirmJoin'),
    0xB019: ('C2ZN_ReqApproveJoin', 0xB01A, 'ZN2C_ResApproveJoin'),
    0xB01E: ('C2ZN_ReqFireClanPlayer', 0xB01F, 'ZN2C_ResFireClanPlayer'),
    0xB020: ('C2ZN_ReqPromoteClanPlayer', 0xB021, 'ZN2C_ResPromoteClanPlayer'),
    0xB022: ('C2ZN_ReqDemoteClanPlayer', 0xB023, 'ZN2C_ResDemoteClanPlayer'),
    0xB024: ('C2ZN_NtfQuitClan', 0xB08E, 'ZN2C_ResQuitClan'),
    0xB025: ('C2ZN_ReqExpandClan', 0xB026, 'ZN2C_ResExpandClan'),
    0xB02B: ('C2ZN_ReqInviteJoin', 0xB02C, 'ZN2C_ResInviteJoin'),
    0xB02D: ('C2ZN_ReqClanPost', 0xB02E, 'ZN2C_ResClanPost'),
    0xB02F: ('C2ZN_ReqUpdateClanPost', 0xB030, 'ZN2C_ResUpdateClanPost'),
    0xB039: ('C2ZN_ReqDismissClan', 0xB03A, 'ZN2C_ResDismissClan'),
    0xB04A: ('C2ZN_ReqBuyClanBadge', 0xB04B, 'ZN2C_ResBuyClanBadge'),
    0xB04E: ('C2ZN_ReqSetClanBadge', 0xB04F, 'ZN2C_ResSetClanBadge'),
    0xB050: ('C2ZN_ReqClanBadge', 0xB049, 'ZN2C_NtfClanBadgeProps'),
    0xB051: ('C2ZN_ReqClanBadgeList', 0xB052, 'ZN2C_ResClanBadgeList'),
    0xB08A: ('C2ZN_ReqSubteams', 0xB08B, 'ZN2C_ResSubteams'),
    0xB080: ('C2ZN_ReqModClanIntroduction', 0xB081, 'ZN2C_ResModClanIntroduction'),
}
SENSITIVE_COMMANDS = {0xB001, 0xB01E, 0xB027, 0xB02A, 0xB039, 0xB03B,
                      0xB042, 0xB053, 0xB054, 0xB055, 0xB056, 0xB074,
                      0xB075, 0xB076, 0xB077, 0xA601}


class ClanService:
    def __init__(self, database, *, is_online=lambda uin: False, send_notification=None):
        self.db = database
        self.is_online = is_online
        self.send_notification = send_notification
        self.badge_catalog = json.loads(Path(__file__).with_name('clan_badge_catalog.json').read_text())

    def _own(self, actor):
        clan = self.db.get_player_clan(actor)
        if clan is None:
            raise ClanDBError('player has no clan', 0x1002)
        return clan

    def _detail(self, clan):
        count = len(self.db.list_clan_members(clan['clan_id']))
        return {
            'Clanid': clan['clan_id'], 'ClanID': clan['clan_id'],
            'ClanName': clan['name'], 'CaptainID': clan['owner_uin'],
            'MaxMembers': clan['max_members'],
            'PackedIcn': clan['packed_icn'], 'PackedFrm': clan['packed_frm'],
            'PackedBkg': clan['packed_bkg'],
            'Count': count, 'CurMembers': count,
            'ClanRecord': {'Level': 1, 'LevelForCalcActivity': 1},
            'Introduction': clan['introduction'],
            'CreateTime': int(datetime.fromisoformat(clan['created_at']).timestamp()),
        }

    def _members(self, rows):
        players = [{'Uin': r['uin'], 'Nickname': r['nickname'] or '',
                    'Role': ROLES[r['role']]}
                   for r in rows]
        return {'Count': len(rows), 'ClanPlayerInfo': players,
                'Status': [2 if self.is_online(r['uin']) else 1 for r in rows],
                'NextLevelPrestige': [0] * len(rows)}

    def _roster_replies(self, clan, is_all=1):
        rows = self.db.list_clan_members(clan['clan_id'])
        chunks = [rows[i:i+200] for i in range(0, len(rows), 200)] or [[]]
        return [(0xB00C, encode('ZN2C_ResClanMembers', {
            'Result': SUCCESS, 'IsAll': is_all, 'TotalCount': len(rows),
            'PackageFlag': (1 if i == 0 else 0) | (2 if i == len(chunks)-1 else 0),
            'MemberInfo': self._members(chunk),
        })) for i, chunk in enumerate(chunks)]

    def _badge_values(self, clan, *, ntf_type=0):
        rows = self.db.list_badges(clan['clan_id'])
        return {'NtfType': ntf_type, 'Count': len(rows),
                # The client keys owned properties by a globally scoped GID.
                # Its other owned-item records use (owner UIN << 32) | local ID;
                # a bare SQLite row ID collides across owners and is rejected by
                # the badge-property cache even when ItemId/Type are valid.
                'BadgePropList': [{'GID': ((int(clan['owner_uin']) & 0xFFFFFFFF) << 32)
                                          | (int(r['gid']) & 0xFFFFFFFF),
                                  'ItemId': r['item_id'],
                                  'ObtainTime': r['obtained_at'], 'AvailbilityHour': r['avail_hours'],
                                  'Type': BADGE_PROP_WIRE_TYPE[r['badge_type']]} for r in rows],
                'PackedIcn': clan['packed_icn'], 'PackedFrm': clan['packed_frm'],
                'PackedBkg': clan['packed_bkg']}

    def refresh_replies(self, actor):
        """Complete menu state after membership or captain edits."""
        clan = self._own(actor)
        return [(0xB08B, encode('ZN2C_ResSubteams', {'Result': SUCCESS})),
                (0xB00A, encode('ZN2C_ResClanDetail', {'Result': SUCCESS, 'ClanDetailedInfo': self._detail(clan)})),
                *self._roster_replies(clan),
                (0xB02E, encode('ZN2C_ResClanPost', {'Result': SUCCESS, 'PostCount': 1, 'Post': [clan['notice']]})),
                (0xB049, encode('ZN2C_NtfClanBadgeProps', self._badge_values(clan)))]

    def disband_replies(self, actor):
        """Clear the stock clan cache after the success reply and ClanID=0."""
        return [(0xB01D, encode('ZN2C_NtfRefreshPlayer', {
                    'PlayerInfoA': {'Uin': actor}, 'PlayerInfoB': {'Uin': actor},
                    'RefreshType': 11})),
                (0xB00C, encode('ZN2C_ResClanMembers', {
                    'Result': SUCCESS, 'IsAll': 1, 'TotalCount': 0,
                    'PackageFlag': 3, 'MemberInfo': self._members([])}))]

    def handle(self, cmd, body, actor):
        """Return replies and UINs whose membership/roster projection changed.

        Malformed or forged requests produce a schema-complete error packet;
        they cannot mutate the database. Multi-approval entries commit separately
        because the native response identifies each request by UIN/message ID.
        """
        req_name, res_cmd, res_name = COMMANDS[cmd]
        req, response, affected = {}, {}, set()
        result_field = 'ErrorCode' if cmd in (0xB02B, 0xB039) else 'Result'
        result_code = SUCCESS
        replies = []
        try:
            req = decode(req_name, body)
            if cmd == 0xB02B:
                response['InvitedUin'] = req['InvitedUin']
            if cmd in (0xB020, 0xB022):
                response['ObjUin'] = req['ObjUin']
            if actor < 10001 or any(req[k] != actor for k in ('Uin', 'ReqUin', 'ManagerUin') if k in req):
                raise ClanDBError('request UIN does not match session', 0x1004)
            if cmd == 0xB019 and any(e['IsAgree'] not in (0, 1) for e in req['PlayerInfoList']):
                raise ClanWireError('invalid approval decision')
            if cmd == 0xB00F:
                response['ClanName'] = req['ClanName']
                try:
                    name = normalize_clan_name(req['ClanName'])
                except ClanDBError as exc:
                    raise ClanDBError('invalid clan name', 0x1005) from exc
                if not self.db.clan_name_available(name):
                    raise ClanDBError('clan name unavailable', 0x1005)
            elif cmd == 0xB001:
                response['ClanName'] = req['ClanName']
                clan = self.db.create_clan(actor, req['ClanName'],
                                           safe_code=req['SafeCode'],
                                           certified_mail=req['CertifiedMail'],
                                           badge_catalog=self.badge_catalog)
                response['ClanName'] = clan['name']
                affected.add(actor)
            elif cmd == 0xB009:
                clan = self._own(actor)
                # The captured reopen path requested detail but never B00B.
                # Return fresh membership after detail, independent of UI cache.
                return [(res_cmd, encode(res_name, {'Result': SUCCESS, 'ClanDetailedInfo': self._detail(clan)})),
                        *self._roster_replies(clan)], affected
            elif cmd == 0xB00B:
                clan = self._own(actor)
                if req['IsAll'] not in (0, 1):
                    raise ClanWireError('invalid roster selection')
                return self._roster_replies(clan, req['IsAll']), affected
            elif cmd == 0xB00D:
                clan = self.db.get_clan(req['ClanID'])
                if not clan:
                    raise ClanDBError('clan missing', 0x1002)
                members = self.db.list_clan_members(clan['clan_id'])
                captain = next(r for r in members if r['uin'] == clan['owner_uin'])
                response.update(ClanCommonInfo=self._detail(clan), CaptainUin=clan['owner_uin'],
                                CaptainName=captain['nickname'] or '')
            elif cmd == 0xB005:
                rows = self.db.search(req['ClanName'])
                response.update(SearchName=req['ClanName'], Count=len(rows),
                                ClanScaleType=req['ClanScaleType'])
                details = [self._detail(r) for r in rows]
                response.update(ClanID=[r['clan_id'] for r in rows],
                                Record=[d['ClanRecord'] for d in details],
                                ClanName=[r['name'] for r in rows],
                                CaptainName=[next(m['nickname'] or '' for m in self.db.list_clan_members(r['clan_id'])
                                                  if m['uin'] == r['owner_uin']) for r in rows],
                                CurMembers=[d['Count'] for d in details],
                                MaxMembers=[r['max_members'] for r in rows],
                                ClanIntroduction=[r['introduction'] for r in rows])
            elif cmd == 0xB013:
                application = self.db.apply(actor, req['ClanID'], req['JoinMsg'])
                clan = self.db.get_clan(req['ClanID'])
                response.update(OfflineMsgId=application['application_id'], ClanID=clan['clan_id'],
                                ClanName=clan['name'])
            elif cmd == 0xB015:
                rows = self.db.applications(actor)
                response.update(Count=len(rows), ClanApproveList=[{
                    'ClanId': r['clan_id'], 'ReqMsgId': r['application_id'],
                    'ReqUin': r['uin'], 'ReqNickname': r['nickname'] or '',
                    'ReqTime': int(datetime.fromisoformat(r['created_at']).timestamp()),
                    'MsgContent': list(r['message'].encode('latin1').ljust(81, b'\0')),
                } for r in rows])
            elif cmd == 0xB02B:
                if not self.is_online(req['InvitedUin']):
                    raise ClanDBError('invited player is offline',0x1018)
                if self.send_notification is None:
                    raise ClanDBError('clan invitation routing is not configured')
                invitation, clan = self.db.invite(actor,req['InvitedUin'])
                notification = encode('ZN2C_NtfInviteJoinClan',dict(
                    ClanID=clan['clan_id'],ClanName=clan['name'],Invite_id=invitation['invite_id']))
                if not self.send_notification(req['InvitedUin'],0xB01C,notification):
                    raise ClanDBError('unable to send clan invitation',0x100A)
            elif cmd == 0xB017:
                if req['IsAgree'] not in (0,1):
                    raise ClanWireError('invalid invitation decision')
                clan, changed = self.db.confirm_invitation(actor,req['ClanID'],req['Invite_id'],req['IsAgree'])
                response.update(OfflineMsgId=req['Invite_id'],ClanID=clan['clan_id'],ClanName=clan['name'])
                # TGOnlineTeamData.OnResConfirmJoin uses 0x1025 for a
                # successfully declined invitation, and 0x9000 for joining.
                result_code = SUCCESS if req['IsAgree'] else 0x1025
                if changed:
                    affected.update(r['uin'] for r in self.db.list_clan_members(clan['clan_id']))
            elif cmd == 0xB019:
                for entry in req['PlayerInfoList']:
                    values = dict(ReqUin=entry['ReqUin'], ReqMsgId=entry['ReqMsgId'])
                    try:
                        cid = self.db.approve(actor, entry['ReqUin'], entry['ReqMsgId'], entry['IsAgree'])
                        values['Result'] = SUCCESS
                        affected.update(r['uin'] for r in self.db.list_clan_members(cid))
                    except ClanDBError as exc:
                        values['Result'] = exc.code
                    replies.append((res_cmd, encode(res_name, values)))
                return replies or [(res_cmd, encode(res_name, {'Result': 0x1008}))], affected
            elif cmd == 0xB024:
                clan = self.db.leave(actor)
                affected.update(r['uin'] for r in self.db.list_clan_members(clan['clan_id']))
                affected.add(actor)
            elif cmd == 0xB01E:
                response['ObjUin'] = req['ObjUin']
                clan = self._own(actor)
                member = next((r for r in self.db.list_clan_members(clan['clan_id']) if r['uin'] == req['ObjUin']), None)
                if member:
                    response['Nickname'] = member['nickname'] or ''
                cid = self.db.kick(actor, req['ObjUin'], req['SafeCode'])
                response['FirePlayerCount'] = 1
                affected.update(r['uin'] for r in self.db.list_clan_members(cid))
                affected.add(req['ObjUin'])
            elif cmd in (0xB020, 0xB022):
                role = next((name for name, value in ROLES.items()
                             if value == req['PlayerNewRole']), None)
                if role is None or role == 'captain':
                    raise ClanDBError('invalid appointment role', 0x1004)
                cid = self.db.set_member_role(actor, req['ObjUin'], role)
                affected.update(r['uin'] for r in self.db.list_clan_members(cid))
            elif cmd == 0xB039:
                affected.update(self.db.disband(actor, req['SafeCode']))
            elif cmd == 0xB080:
                cid = self.db.update_introduction(actor, req['Introduction'])
                response['Introduction'] = req['Introduction']
                affected.update(r['uin'] for r in self.db.list_clan_members(cid))
            elif cmd == 0xB08A:
                self._own(actor)
                # No named subteams have been created by this implementation.
                response['Count'] = 0
            elif cmd == 0xB02D:
                clan = self._own(actor)
                response.update(PostCount=1, Post=[clan['notice']])
            elif cmd == 0xB02F:
                response['PostIndex'] = req['PostIndex']
                cid = self.db.update_notice(actor, req['PostIndex'], req['Post'])
                affected.update(r['uin'] for r in self.db.list_clan_members(cid))
            elif cmd == 0xB025:
                response['TotalPlayerCount'] = self._own(actor)['max_members']
                cid = self.db.expand(actor, req['TotalPlayerCount'])
                response['TotalPlayerCount'] = req['TotalPlayerCount']
                affected.update(r['uin'] for r in self.db.list_clan_members(cid))
            elif cmd == 0xB04A:
                cid = self.db.buy_badges(actor, req['PayType'], req['CommodityList'], self.badge_catalog)
                affected.update(r['uin'] for r in self.db.list_clan_members(cid))
                replies.append((0xB049, encode('ZN2C_NtfClanBadgeProps', self._badge_values(self._own(actor)))))
            elif cmd == 0xB04E:
                if req['Count'] > 3:
                    raise ClanWireError('invalid component count')
                cid = self.db.set_badge(actor, req['PackedIcn'], req['PackedFrm'], req['PackedBkg'])
                affected.update(r['uin'] for r in self.db.list_clan_members(cid))
                replies.append((0xB049, encode('ZN2C_NtfClanBadgeProps', self._badge_values(self._own(actor), ntf_type=1))))
            elif cmd == 0xB050:
                return [(res_cmd, encode(res_name, self._badge_values(self._own(actor))))], affected
            elif cmd == 0xB051:
                clans = [self.db.get_player_clan(uin) for uin in req['UinList']]
                response.update(Count=req['Count'], UinList=req['UinList'],
                                IcnIDList=[c['packed_icn'] if c else -1 for c in clans],
                                FrmIDList=[c['packed_frm'] if c else -1 for c in clans],
                                BkgIDList=[c['packed_bkg'] if c else -1 for c in clans])
            response[result_field] = result_code
        except (ClanDBError, ClanWireError, sqlite3.Error) as exc:
            # A badge inventory notification has no Result field. Do not send
            # an empty success-looking inventory for a forged/malformed request.
            if cmd in (0xB050, 0xB051):
                return [], set()
            response[result_field] = getattr(exc, 'code', 0x1008)
        return [(res_cmd, encode(res_name, response)), *replies], affected
