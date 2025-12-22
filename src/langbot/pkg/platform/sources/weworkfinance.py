"""
企业微信会话存档适配器 - 轮询模式

通过 py-weworkfinancesdk 轮询拉取会话存档消息，将消息注入到 langbot pipeline 中处理。
"""
from __future__ import annotations
import typing
import asyncio
import traceback
import json
import base64
import datetime
import os
import time

import langbot_plugin.api.definition.abstract.platform.adapter as abstract_platform_adapter
import langbot_plugin.api.entities.builtin.platform.message as platform_message
import langbot_plugin.api.entities.builtin.platform.events as platform_events
import langbot_plugin.api.entities.builtin.platform.entities as platform_entities
from ..logger import EventLogger

# 导入 py-weworkfinancesdk
try:
    from py_weworkfinancesdk import SDK, decrypt_data
    WEWORK_SDK_AVAILABLE = True
except ImportError:
    WEWORK_SDK_AVAILABLE = False
    SDK = None
    decrypt_data = None

# 导入 RSA 解密库
try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.backends import default_backend
    CRYPTOGRAPHY_AVAILABLE = True
except ImportError:
    CRYPTOGRAPHY_AVAILABLE = False


class WeWorkFinanceMessageConverter(abstract_platform_adapter.AbstractMessageConverter):
    """企业微信会话存档消息转换器"""

    @staticmethod
    async def target2yiri(decrypted_msg: dict) -> platform_message.MessageChain:
        """
        将企微会话存档解密后的消息转换为 langbot 标准 MessageChain

        Args:
            decrypted_msg: 解密后的消息 JSON，格式如：
                {
                    "msgid": "...",
                    "action": "send",
                    "from": "WuChen",
                    "tolist": ["wmYkr6IQAALSL9lN6FyzmrWJrXjtaLvw"],
                    "roomid": "",
                    "msgtime": 1765872167674,
                    "msgtype": "text",
                    "text": {"content": "111"}
                }

        Returns:
            platform_message.MessageChain
        """
        yiri_msg_list = []

        # 添加消息源信息
        msgid = decrypted_msg.get('msgid', '')
        msgtime = decrypted_msg.get('msgtime', 0)
        # msgtime 是毫秒时间戳，转为 datetime
        msg_datetime = datetime.datetime.fromtimestamp(msgtime / 1000.0) if msgtime else datetime.datetime.now()

        yiri_msg_list.append(platform_message.Source(id=msgid, time=msg_datetime))

        # 根据消息类型转换
        msgtype = decrypted_msg.get('msgtype', 'text')

        if msgtype == 'text':
            # 文本消息
            text_content = decrypted_msg.get('text', {}).get('content', '')
            if text_content:
                yiri_msg_list.append(platform_message.Plain(text=text_content))

        elif msgtype == 'image':
            # 图片消息 - 支持下载
            image_info = decrypted_msg.get('image', {})
            sdkfileid = image_info.get('sdkfileid', '')
            if sdkfileid:
                # 标记需要下载，稍后在 adapter 中处理
                yiri_msg_list.append(platform_message.Plain(text=f'__DOWNLOAD_IMAGE__:{sdkfileid}'))
            else:
                yiri_msg_list.append(platform_message.Unknown(text='[图片消息]'))

        elif msgtype == 'voice':
            # 语音消息
            voice_info = decrypted_msg.get('voice', {})
            sdkfileid = voice_info.get('sdkfileid', '')
            if sdkfileid:
                yiri_msg_list.append(platform_message.Plain(text=f'[语音消息: {sdkfileid}]'))
            else:
                yiri_msg_list.append(platform_message.Unknown(text='[语音消息]'))

        elif msgtype == 'video':
            # 视频消息
            video_info = decrypted_msg.get('video', {})
            sdkfileid = video_info.get('sdkfileid', '')
            if sdkfileid:
                yiri_msg_list.append(platform_message.Plain(text=f'[视频消息: {sdkfileid}]'))
            else:
                yiri_msg_list.append(platform_message.Unknown(text='[视频消息]'))

        elif msgtype == 'file':
            # 文件消息
            file_info = decrypted_msg.get('file', {})
            filename = file_info.get('filename', '')
            sdkfileid = file_info.get('sdkfileid', '')
            if filename:
                yiri_msg_list.append(platform_message.Plain(text=f'[文件: {filename}]'))
            elif sdkfileid:
                yiri_msg_list.append(platform_message.Plain(text=f'[文件消息: {sdkfileid}]'))
            else:
                yiri_msg_list.append(platform_message.Unknown(text='[文件消息]'))

        elif msgtype == 'link':
            # 链接消息
            link_info = decrypted_msg.get('link', {})
            title = link_info.get('title', '')
            link_url = link_info.get('link_url', '')
            desc = link_info.get('description', '')
            if title or link_url:
                text = f'[链接: {title}]({link_url})'
                if desc:
                    text += f'\n{desc}'
                yiri_msg_list.append(platform_message.Plain(text=text))
            else:
                yiri_msg_list.append(platform_message.Unknown(text='[链接消息]'))

        elif msgtype == 'weapp':
            # 小程序消息
            weapp_info = decrypted_msg.get('weapp', {})
            title = weapp_info.get('title', '')
            yiri_msg_list.append(platform_message.Plain(text=f'[小程序: {title}]'))

        elif msgtype == 'emotion':
            # 表情消息
            yiri_msg_list.append(platform_message.Plain(text='[表情]'))

        elif msgtype == 'location':
            # 位置消息
            location_info = decrypted_msg.get('location', {})
            title = location_info.get('title', '')
            address = location_info.get('address', '')
            yiri_msg_list.append(platform_message.Plain(text=f'[位置: {title} - {address}]'))

        elif msgtype == 'mixed':
            # 混合消息（图文消息）
            mixed_info = decrypted_msg.get('mixed', {})
            # mixed 消息包含 item 列表
            items = mixed_info.get('item', [])
            for item in items:
                item_type = item.get('type', '')
                if item_type == 'text':
                    content = item.get('content', '')
                    if content:
                        yiri_msg_list.append(platform_message.Plain(text=content))
                elif item_type == 'image':
                    sdkfileid = item.get('sdkfileid', '')
                    yiri_msg_list.append(platform_message.Plain(text=f'[图片: {sdkfileid}]'))
                # 可以继续处理其他类型

        else:
            # 未知消息类型
            yiri_msg_list.append(platform_message.Unknown(text=f'[不支持的消息类型: {msgtype}]'))

        # 如果没有有效内容，添加一个占位符
        has_content = any(
            not isinstance(element, (platform_message.Source, platform_message.At))
            for element in yiri_msg_list
        )
        if not has_content:
            yiri_msg_list.append(platform_message.Unknown(text=f'[消息类型: {msgtype}]'))

        return platform_message.MessageChain(yiri_msg_list)

    @staticmethod
    async def yiri2target(message_chain: platform_message.MessageChain) -> str:
        """
        将 langbot MessageChain 转换为企微格式（用于回复）
        由于我们不需要回复功能，这里简单转为文本即可
        """
        text_parts = []
        for component in message_chain:
            if isinstance(component, platform_message.Plain):
                text_parts.append(component.text)
            elif isinstance(component, platform_message.Image):
                text_parts.append('[图片]')
            elif isinstance(component, platform_message.At):
                text_parts.append(f'@{component.target}')
        return ''.join(text_parts)


class WeWorkFinanceEventConverter(abstract_platform_adapter.AbstractEventConverter):
    """企业微信会话存档事件转换器"""

    @staticmethod
    async def target2yiri(decrypted_msg: dict) -> typing.Optional[platform_events.MessageEvent]:
        """
        将企微会话存档消息转换为 langbot 事件

        Args:
            decrypted_msg: 解密后的消息 JSON

        Returns:
            FriendMessage 或 GroupMessage 或 None
        """
        # 转换消息链
        message_chain = await WeWorkFinanceMessageConverter.target2yiri(decrypted_msg)

        # 提取信息
        from_user = decrypted_msg.get('from', '')
        tolist = decrypted_msg.get('tolist', [])
        roomid = decrypted_msg.get('roomid', '')
        msgtime = decrypted_msg.get('msgtime', 0)
        action = decrypted_msg.get('action', 'send')

        # 只处理发送消息（send），忽略撤回等操作
        if action != 'send':
            return None

        # 转换时间戳（毫秒 -> 秒）
        timestamp = msgtime / 1000.0 if msgtime else datetime.datetime.now().timestamp()

        # 判断是群聊还是私聊
        if roomid:
            # 群聊消息
            sender = platform_entities.GroupMember(
                id=from_user,
                permission='MEMBER',
                member_name=from_user,  # 会话存档中没有昵称，用 userid
                group=platform_entities.Group(
                    id=roomid,
                    name=roomid,  # 会话存档中没有群名称，用 roomid
                    permission=platform_entities.Permission.Member,
                ),
                special_title='',
            )
            return platform_events.GroupMessage(
                sender=sender,
                message_chain=message_chain,
                time=timestamp,
                source_platform_object=decrypted_msg,
            )
        else:
            # 私聊消息
            friend = platform_entities.Friend(
                id=from_user,
                nickname=from_user,  # 会话存档中没有昵称，用 userid
                remark='',
            )
            return platform_events.FriendMessage(
                sender=friend,
                message_chain=message_chain,
                time=timestamp,
                source_platform_object=decrypted_msg,
            )


class WeWorkFinanceAdapter(abstract_platform_adapter.AbstractMessagePlatformAdapter):
    """
    企业微信会话存档适配器

    通过轮询方式拉取会话存档消息，将消息注入到 langbot pipeline 中处理。

    配置项：
        corpid: 企业 ID
        secret: 会话存档的 secret
        contact_secret: 通讯录管理的 secret（可选，用于获取成员列表）
        rsa_private_key_path: RSA 私钥文件路径（用于解密消息）
        polling_interval: 轮询间隔（秒），默认 5
        batch_limit: 每次拉取的消息数量，默认 100
        timeout: API 超时时间（秒），默认 10
        seq_save_path: seq 游标保存路径，默认 data/wework_finance_seq.txt
        member_cache_ttl: 成员列表缓存时间（秒），默认 864000（10天）
        member_check_interval: 成员列表检查间隔（秒），默认 86400（1天）
        internal_users: 内部员工列表（可选，作为降级方案）
    """

    sdk: typing.Optional[SDK] = None
    rsa_private_key: typing.Any = None
    seq: int = 0
    config: dict
    logger: EventLogger

    # 事件回调存储
    _friend_message_callbacks: list[typing.Callable]
    _group_message_callbacks: list[typing.Callable]

    # 轮询任务
    _polling_task: typing.Optional[asyncio.Task] = None
    _running: bool = False

    # 成员列表管理
    _internal_members: typing.Set[str] = set()
    _members_last_update: float = 0
    _contact_access_token: typing.Optional[str] = None
    _contact_token_expires: float = 0

    def __init__(self, config: dict, logger: EventLogger):
        # 检查依赖
        if not WEWORK_SDK_AVAILABLE:
            raise Exception('py-weworkfinancesdk 未安装，请运行: pip install py-weworkfinancesdk')

        # 校验必填项
        required_keys = ['corpid', 'secret']
        missing_keys = [key for key in required_keys if key not in config]
        if missing_keys:
            raise Exception(f'WeWorkFinance 缺少配置项: {missing_keys}')
        
        super().__init__(
            config=config,
            logger=logger,
            bot=None,
            bot_account_id=config.get('corpid', ''),
        )
        
        self.config = config
        self.logger = logger
        self._friend_message_callbacks = []
        self._group_message_callbacks = []

        # 初始化成员列表管理
        self._internal_members = set()
        self._members_last_update = 0
        self._contact_access_token = None
        self._contact_token_expires = 0

        # 初始化 SDK
        try:
            self.sdk = SDK(
                corpid=config['corpid'],
                secret=config['secret']
            )
            logger.info('WeWorkFinance SDK 初始化成功')
        except Exception as e:
            raise Exception(f'WeWorkFinance SDK 初始化失败: {e}')

        # 加载 RSA 私钥
        self._load_rsa_private_key()

        # 加载 seq 游标
        self._load_seq()


    def _load_rsa_private_key(self):
        """加载 RSA 私钥用于解密消息"""
        if not CRYPTOGRAPHY_AVAILABLE:
            self.logger.warning('cryptography 库未安装，无法解密消息内容')
            return

        rsa_key_path = self.config.get('rsa_private_key_path', '')
        if not rsa_key_path:
            # 尝试默认路径
            default_paths = [
                'rsa_private.pem',
                'data/rsa_private.pem',
                os.path.expanduser('~/.wework/rsa_private.pem'),
            ]
            for path in default_paths:
                if os.path.exists(path):
                    rsa_key_path = path
                    break

        if not rsa_key_path or not os.path.exists(rsa_key_path):
            self.logger.warning(f'未找到 RSA 私钥文件，消息内容将无法解密。请配置 rsa_private_key_path')
            return

        try:
            with open(rsa_key_path, 'rb') as f:
                key_data = f.read()

            # 尝试加载私钥
            password = os.environ.get('RSA_PRIVATE_KEY_PASSWORD')
            if password and isinstance(password, str):
                password = password.encode('utf-8')

            self.rsa_private_key = serialization.load_pem_private_key(
                key_data,
                password=password,
                backend=default_backend()
            )
            self.logger.info(f'已加载 RSA 私钥: {rsa_key_path}')
        except Exception as e:
            self.logger.error(f'加载 RSA 私钥失败: {e}')

    def _load_seq(self):
        """从文件加载 seq 游标"""
        seq_save_path = self.config.get('seq_save_path', 'data/wework_finance_seq.txt')

        # 确保目录存在
        os.makedirs(os.path.dirname(seq_save_path) if os.path.dirname(seq_save_path) else '.', exist_ok=True)

        if os.path.exists(seq_save_path):
            try:
                with open(seq_save_path, 'r') as f:
                    self.seq = int(f.read().strip())
                self.logger.info(f'已加载 seq 游标: {self.seq}')
            except Exception as e:
                self.logger.warning(f'加载 seq 游标失败: {e}，将从 0 开始')
                self.seq = 0
        else:
            self.seq = 0
            self.logger.info('未找到 seq 游标文件，将从 0 开始')

    def _save_seq(self):
        """保存 seq 游标到文件"""
        seq_save_path = self.config.get('seq_save_path', 'data/wework_finance_seq.txt')
        try:
            with open(seq_save_path, 'w') as f:
                f.write(str(self.seq))
        except Exception as e:
            self.logger.error(f'保存 seq 游标失败: {e}')

    async def _get_contact_access_token(self) -> typing.Optional[str]:
        """获取通讯录管理的 access_token"""
        now = time.time()
        if self._contact_access_token and now < self._contact_token_expires:
            return self._contact_access_token

        contact_secret = self.config.get('contact_secret')
        if not contact_secret:
            return None

        try:
            import aiohttp
            url = 'https://qyapi.weixin.qq.com/cgi-bin/gettoken'
            params = {'corpid': self.config['corpid'], 'corpsecret': contact_secret}

            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params) as resp:
                    result = await resp.json()

            if result.get('errcode') != 0:
                await self.logger.error(f'获取 access_token 失败: {result.get("errmsg")}')
                return None

            self._contact_access_token = result['access_token']
            self._contact_token_expires = now + result.get('expires_in', 7200) - 300
            await self.logger.info('成功获取通讯录 access_token')
            return self._contact_access_token
        except Exception as e:
            await self.logger.error(f'获取 access_token 失败: {e}')
            return None

    async def _fetch_department_members(self, access_token: str, department_id: int = 1) -> list:
        """获取指定部门的成员列表"""
        try:
            import aiohttp
            url = 'https://qyapi.weixin.qq.com/cgi-bin/user/simplelist'
            params = {'access_token': access_token, 'department_id': department_id, 'fetch_child': 1}

            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params) as resp:
                    result = await resp.json()

            if result.get('errcode') != 0:
                await self.logger.error(f'获取部门成员失败: {result.get("errmsg")}')
                return []

            return result.get('userlist', [])
        except Exception as e:
            await self.logger.error(f'获取部门成员异常: {e}')
            return []

    async def _update_internal_members(self) -> bool:
        """更新内部成员列表（优先 API，降级到配置）"""
        now = time.time()
        cache_ttl = self.config.get('member_cache_ttl', 864000)  # 默认 10 天

        if self._internal_members and (now - self._members_last_update) < cache_ttl:
            return True

        await self.logger.info('开始更新内部成员列表...')

        # 尝试通过 API 获取
        access_token = await self._get_contact_access_token()
        if access_token:
            members = await self._fetch_department_members(access_token, department_id=1)
            if members:
                self._internal_members = {member['userid'] for member in members}
                self._members_last_update = now
                await self.logger.info(f'成员列表更新成功（API）: {len(self._internal_members)} 人')
                return True

        # API 失败，使用配置的成员列表
        configured_members = self.config.get('internal_users', [])
        if configured_members:
            self._internal_members = set(configured_members)
            self._members_last_update = now
            await self.logger.info(f'使用配置的成员列表: {len(self._internal_members)} 人')
            return True

        await self.logger.warning('未能获取成员列表（API 和配置都不可用），将使用 ID 格式判断')
        return False

    def _is_external_contact_by_format(self, user_id: str) -> bool:
        """基于 ID 格式判断是否为外部联系人"""
        if len(user_id) <= 15:
            return False
        has_upper = any(c.isupper() for c in user_id)
        has_lower = any(c.islower() for c in user_id)
        return has_upper and has_lower

    def _is_internal_member(self, user_id: str) -> bool:
        """判断是否为内部成员（优先成员列表，降级到格式判断）"""
        if self._internal_members:
            return user_id in self._internal_members
        return not self._is_external_contact_by_format(user_id)

    def _should_process_message(self, decrypted_msg: dict) -> tuple[bool, typing.Optional[str]]:
        """判断是否应该处理该消息（只处理外部→内部）"""
        from_user = decrypted_msg.get('from', '')
        tolist = decrypted_msg.get('tolist', [])

        if self._is_internal_member(from_user):
            return False, None

        for to_user in tolist:
            if self._is_internal_member(to_user):
                return True, to_user

        return False, None

    async def _decrypt_message(self, msg: dict) -> typing.Optional[dict]:
        """
        解密单条消息

        Args:
            msg: 原始消息，包含 encrypt_random_key 和 encrypt_chat_msg

        Returns:
            解密后的消息 JSON dict，失败返回 None
        """
        if not self.rsa_private_key:
            self.logger.warning('未加载 RSA 私钥，无法解密消息')
            return None

        try:
            encrypt_random_key = msg.get('encrypt_random_key', '')
            encrypt_chat_msg = msg.get('encrypt_chat_msg', '')

            if not encrypt_random_key or not encrypt_chat_msg:
                self.logger.warning('消息缺少加密字段')
                return None

            # 1. 解密 encrypt_random_key
            encrypted_key_bytes = base64.b64decode(encrypt_random_key)
            decrypt_key = self.rsa_private_key.decrypt(encrypted_key_bytes, padding.PKCS1v15())
            encrypt_key = decrypt_key.decode('utf-8')

            # 2. 使用 encrypt_key 解密消息内容
            decrypted_msg_bytes = decrypt_data(encrypt_key=encrypt_key, encrypt_msg=encrypt_chat_msg)

            # 3. 解析 JSON
            decrypted_json = json.loads(decrypted_msg_bytes.decode('utf-8'))
            return decrypted_json

        except Exception as e:
            self.logger.error(f'解密消息失败: {e}\n{traceback.format_exc()}')
            return None

    async def _polling_loop(self):
        """后台轮询任务"""
        polling_interval = self.config.get('polling_interval', 5)
        batch_limit = self.config.get('batch_limit', 100)
        timeout = self.config.get('timeout', 10)
        member_check_interval = self.config.get('member_check_interval', 86400)  # 默认 1 天
        loop_count = 0

        # 计算多少次轮询检查一次成员列表（避免除以零）
        check_loops = max(1, int(member_check_interval / polling_interval))

        await self.logger.info(
            f'开始轮询会话存档消息 (interval={polling_interval}s, limit={batch_limit}, '
            f'member_check_interval={member_check_interval}s, check_every={check_loops}_loops)'
        )

        while self._running:
            try:
                # 定期检查成员列表是否需要更新
                if loop_count % check_loops == 0 and loop_count > 0:
                    await self._update_internal_members()

                loop_count += 1

                # 拉取消息
                result = self.sdk.get_chat_data(seq=self.seq, limit=batch_limit, timeout=timeout)

                # 处理返回结果
                if isinstance(result, bytes):
                    result = json.loads(result.decode('utf-8'))

                errcode = result.get('errcode', 0)
                if errcode != 0:
                    errmsg = result.get('errmsg', '')
                    await self.logger.error(f'拉取消息失败: errcode={errcode}, errmsg={errmsg}')
                    await asyncio.sleep(polling_interval)
                    continue

                chatdata = result.get('chatdata', [])
                if chatdata:
                    await self.logger.info(f'拉取到 {len(chatdata)} 条新消息')

                    processed_count = 0
                    for msg in chatdata:
                        # 解密消息
                        decrypted_msg = await self._decrypt_message(msg)
                        if not decrypted_msg:
                            continue

                        # 检查是否应该处理该消息（只处理外部→内部）
                        should_process, internal_recipient = self._should_process_message(decrypted_msg)
                        if not should_process:
                            continue

                        # 在消息中附加接收者信息
                        decrypted_msg['_internal_recipient'] = internal_recipient

                        # 转换为事件
                        event = await WeWorkFinanceEventConverter.target2yiri(decrypted_msg)
                        if not event:
                            continue

                        # 分发事件
                        await self._dispatch_event(event)
                        processed_count += 1

                    if processed_count > 0:
                        await self.logger.info(f'处理了 {processed_count} 条有效消息（外部→内部）')

                    # 更新 seq 游标
                    max_seq = max(msg.get('seq', 0) for msg in chatdata)
                    if max_seq > self.seq:
                        self.seq = max_seq
                        self._save_seq()
                        await self.logger.info(f'更新 seq 游标: {self.seq}')

                # 等待下一次轮询
                await asyncio.sleep(polling_interval)

            except asyncio.CancelledError:
                await self.logger.info('轮询任务被取消')
                break
            except Exception as e:
                await self.logger.error(f'轮询错误: {e}\n{traceback.format_exc()}')
                await asyncio.sleep(10)  # 出错后等待更长时间

        await self.logger.info('轮询任务结束')

    async def _dispatch_event(self, event: platform_events.MessageEvent):
        """分发事件到已注册的回调"""
        callbacks = []

        if isinstance(event, platform_events.FriendMessage):
            callbacks = self._friend_message_callbacks
        elif isinstance(event, platform_events.GroupMessage):
            callbacks = self._group_message_callbacks

        for callback in callbacks:
            try:
                await callback(event, self)
            except Exception as e:
                await self.logger.error(f'回调执行错误: {e}\n{traceback.format_exc()}')

    def register_listener(
        self,
        event_type: typing.Type[platform_events.Event],
        callback: typing.Callable[
            [platform_events.Event, abstract_platform_adapter.AbstractMessagePlatformAdapter], None
        ],
    ):
        """注册事件监听器"""
        if event_type == platform_events.FriendMessage:
            self._friend_message_callbacks.append(callback)
            asyncio.create_task(self.logger.info('已注册 FriendMessage 监听器'))
        elif event_type == platform_events.GroupMessage:
            self._group_message_callbacks.append(callback)
            asyncio.create_task(self.logger.info('已注册 GroupMessage 监听器'))

    async def reply_message(
        self,
        message_source: platform_events.MessageEvent,
        message: platform_message.MessageChain,
        quote_origin: bool = False,
    ):
        """
        发送回复消息

        由于会话存档是只读的，这里不执行实际发送操作。
        回复消息应该通过其他方式处理（如 Redis 插件存储后由外部系统发送）。
        """
        # 记录日志，但不实际发送
        content = await WeWorkFinanceMessageConverter.yiri2target(message)
        await self.logger.info(f'[不发送] 回复消息: {content}')

    async def send_message(self, target_type: str, target_id: str, message: platform_message.MessageChain):
        """主动发送消息（不支持）"""
        await self.logger.warning('WeWorkFinance 适配器不支持主动发送消息')

    async def run_async(self):
        """启动适配器 - 开始轮询任务"""
        self._running = True
        await self.logger.info('WeWorkFinance 适配器启动')

        # 首次拉取成员列表
        await self._update_internal_members()

        # 创建轮询任务
        self._polling_task = asyncio.create_task(self._polling_loop())

        # 等待任务完成
        try:
            await self._polling_task
        except asyncio.CancelledError:
            await self.logger.info('轮询任务被取消')
        finally:
            self._running = False

    async def kill(self) -> bool:
        """停止适配器"""
        await self.logger.info('停止 WeWorkFinance 适配器')
        self._running = False

        if self._polling_task and not self._polling_task.done():
            self._polling_task.cancel()
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass

        # 清理 SDK
        if self.sdk:
            try:
                del self.sdk
            except Exception:
                pass

        return True

    async def unregister_listener(
        self,
        event_type: type,
        callback: typing.Callable[
            [platform_events.Event, abstract_platform_adapter.AbstractMessagePlatformAdapter], None
        ],
    ):
        """取消注册事件监听器"""
        if event_type == platform_events.FriendMessage and callback in self._friend_message_callbacks:
            self._friend_message_callbacks.remove(callback)
        elif event_type == platform_events.GroupMessage and callback in self._group_message_callbacks:
            self._group_message_callbacks.remove(callback)

    async def is_muted(self, group_id: int) -> bool:
        """检查群是否被禁言（不支持）"""
        return False
