"""
企业微信会话存档适配器 - 媒体文件增强版

在 weworkfinance.py 基础上，增加完整的媒体文件（图片/语音/视频/文件）下载支持。
"""
import base64
import asyncio
from typing import Optional

# 继承原始适配器
from .weworkfinance import (
    WeWorkFinanceAdapter,
    WeWorkFinanceMessageConverter as BaseMessageConverter,
    WeWorkFinanceEventConverter,
    WEWORK_SDK_AVAILABLE,
    SDK,
)

import langbot_plugin.api.entities.builtin.platform.message as platform_message


class MediaDownloadHelper:
    """媒体文件下载辅助类"""

    def __init__(self, sdk: SDK, logger):
        self.sdk = sdk
        self.logger = logger
        self._download_cache = {}  # 简单的内存缓存

    async def download_media(self, sdkfileid: str, timeout: int = 30) -> Optional[bytes]:
        """
        下载媒体文件

        Args:
            sdkfileid: 媒体文件 ID
            timeout: 超时时间（秒）

        Returns:
            文件字节数据，失败返回 None
        """
        # 检查缓存
        if sdkfileid in self._download_cache:
            return self._download_cache[sdkfileid]

        try:
            await self.logger.info(f'开始下载媒体文件: {sdkfileid[:20]}...')

            # 分片下载
            all_data = bytearray()
            indexbuf = ''  # 初始为空字符串

            max_chunks = 100  # 最多下载 100 个分片（防止无限循环）
            chunk_count = 0

            while chunk_count < max_chunks:
                # 调用 SDK 下载
                result = self.sdk.get_media_data(
                    sdk_fileid=sdkfileid,
                    indexbuf=indexbuf,
                    timeout=timeout
                )

                # 解析返回结果
                if isinstance(result, dict):
                    data = result.get('data', b'')
                    outindexbuf = result.get('outindexbuf', '')
                    is_finish = result.get('is_finish', 0)

                    # 追加数据
                    if data:
                        all_data.extend(data)

                    chunk_count += 1

                    # 检查是否下载完成
                    if is_finish == 1:
                        await self.logger.info(
                            f'媒体文件下载完成: {sdkfileid[:20]}... (大小: {len(all_data)} bytes, 分片: {chunk_count})'
                        )
                        # 缓存结果
                        self._download_cache[sdkfileid] = bytes(all_data)
                        return bytes(all_data)

                    # 更新索引继续下载
                    indexbuf = outindexbuf

                else:
                    await self.logger.error(f'媒体文件下载失败: 返回格式错误')
                    return None

            # 超过最大分片数
            await self.logger.warning(f'媒体文件下载超过最大分片数: {sdkfileid[:20]}...')
            return bytes(all_data) if all_data else None

        except Exception as e:
            await self.logger.error(f'媒体文件下载异常: {sdkfileid[:20]}... - {e}')
            return None

    def clear_cache(self):
        """清空下载缓存（释放内存）"""
        cleared = len(self._download_cache)
        self._download_cache.clear()
        return cleared


class WeWorkFinanceMediaMessageConverter(BaseMessageConverter):
    """支持媒体文件下载的消息转换器"""

    @staticmethod
    async def target2yiri(decrypted_msg: dict, media_helper: Optional[MediaDownloadHelper] = None) -> platform_message.MessageChain:
        """
        将企微会话存档解密后的消息转换为 langbot 标准 MessageChain
        增强版：支持下载图片/语音/视频/文件

        Args:
            decrypted_msg: 解密后的消息 JSON
            media_helper: 媒体下载辅助对象（如果提供，则下载媒体文件）

        Returns:
            platform_message.MessageChain
        """
        import datetime

        yiri_msg_list = []

        # 添加消息源信息
        msgid = decrypted_msg.get('msgid', '')
        msgtime = decrypted_msg.get('msgtime', 0)
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
            # 图片消息 - 支持完整下载
            image_info = decrypted_msg.get('image', {})
            sdkfileid = image_info.get('sdkfileid', '')

            if sdkfileid and media_helper:
                # 下载图片
                image_bytes = await media_helper.download_media(sdkfileid)
                if image_bytes:
                    # 转为 base64
                    image_base64 = base64.b64encode(image_bytes).decode()
                    # 尝试识别图片格式（简单判断）
                    image_format = 'png'  # 默认
                    if image_bytes.startswith(b'\xff\xd8\xff'):
                        image_format = 'jpeg'
                    elif image_bytes.startswith(b'\x89PNG'):
                        image_format = 'png'
                    elif image_bytes.startswith(b'GIF'):
                        image_format = 'gif'

                    yiri_msg_list.append(
                        platform_message.Image(base64=f'data:image/{image_format};base64,{image_base64}')
                    )
                else:
                    # 下载失败，降级为文本
                    yiri_msg_list.append(platform_message.Plain(text=f'[图片下载失败: {sdkfileid[:20]}...]'))
            elif sdkfileid:
                # 没有 media_helper，只显示 ID
                yiri_msg_list.append(platform_message.Plain(text=f'[图片: {sdkfileid[:20]}...]'))
            else:
                yiri_msg_list.append(platform_message.Unknown(text='[图片消息]'))

        elif msgtype == 'voice':
            # 语音消息 - 支持完整下载
            voice_info = decrypted_msg.get('voice', {})
            sdkfileid = voice_info.get('sdkfileid', '')
            voice_size = voice_info.get('voice_size', 0)
            play_length = voice_info.get('play_length', 0)

            if sdkfileid and media_helper:
                # 下载语音
                voice_bytes = await media_helper.download_media(sdkfileid)
                if voice_bytes:
                    # 转为 base64
                    voice_base64 = base64.b64encode(voice_bytes).decode()
                    yiri_msg_list.append(
                        platform_message.Voice(
                            base64=f'data:audio/amr;base64,{voice_base64}',
                            length=play_length
                        )
                    )
                else:
                    yiri_msg_list.append(platform_message.Plain(text=f'[语音下载失败: {sdkfileid[:20]}...]'))
            elif sdkfileid:
                yiri_msg_list.append(platform_message.Plain(text=f'[语音: {play_length}秒]'))
            else:
                yiri_msg_list.append(platform_message.Unknown(text='[语音消息]'))

        elif msgtype == 'video':
            # 视频消息 - 支持完整下载
            video_info = decrypted_msg.get('video', {})
            sdkfileid = video_info.get('sdkfileid', '')
            filesize = video_info.get('filesize', 0)
            play_length = video_info.get('play_length', 0)

            if sdkfileid and media_helper:
                # 下载视频
                video_bytes = await media_helper.download_media(sdkfileid, timeout=60)  # 视频可能较大
                if video_bytes:
                    # 转为 base64（注意：视频可能很大）
                    video_base64 = base64.b64encode(video_bytes).decode()
                    # 使用 File 类型传递视频（langbot 可能没有专门的 Video 类型）
                    yiri_msg_list.append(
                        platform_message.File(
                            url=f'data:video/mp4;base64,{video_base64}',
                            name=f'video_{msgid[:10]}.mp4',
                            size=len(video_bytes)
                        )
                    )
                else:
                    yiri_msg_list.append(platform_message.Plain(text=f'[视频下载失败: {sdkfileid[:20]}...]'))
            elif sdkfileid:
                yiri_msg_list.append(platform_message.Plain(text=f'[视频: {filesize} bytes, {play_length}秒]'))
            else:
                yiri_msg_list.append(platform_message.Unknown(text='[视频消息]'))

        elif msgtype == 'file':
            # 文件消息 - 支持完整下载
            file_info = decrypted_msg.get('file', {})
            sdkfileid = file_info.get('sdkfileid', '')
            filename = file_info.get('filename', 'file')
            filesize = file_info.get('filesize', 0)

            if sdkfileid and media_helper:
                # 下载文件
                file_bytes = await media_helper.download_media(sdkfileid, timeout=60)
                if file_bytes:
                    # 转为 base64
                    file_base64 = base64.b64encode(file_bytes).decode()
                    yiri_msg_list.append(
                        platform_message.File(
                            url=f'data:application/octet-stream;base64,{file_base64}',
                            name=filename,
                            size=len(file_bytes)
                        )
                    )
                else:
                    yiri_msg_list.append(platform_message.Plain(text=f'[文件下载失败: {filename}]'))
            elif filename:
                yiri_msg_list.append(platform_message.Plain(text=f'[文件: {filename}, {filesize} bytes]'))
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
            emotion_info = decrypted_msg.get('emotion', {})
            sdkfileid = emotion_info.get('sdkfileid', '')

            if sdkfileid and media_helper:
                # 下载表情图片
                emotion_bytes = await media_helper.download_media(sdkfileid)
                if emotion_bytes:
                    emotion_base64 = base64.b64encode(emotion_bytes).decode()
                    yiri_msg_list.append(
                        platform_message.Image(base64=f'data:image/gif;base64,{emotion_base64}')
                    )
                else:
                    yiri_msg_list.append(platform_message.Plain(text='[表情]'))
            else:
                yiri_msg_list.append(platform_message.Plain(text='[表情]'))

        elif msgtype == 'location':
            # 位置消息
            location_info = decrypted_msg.get('location', {})
            title = location_info.get('title', '')
            address = location_info.get('address', '')
            latitude = location_info.get('latitude', 0)
            longitude = location_info.get('longitude', 0)
            yiri_msg_list.append(
                platform_message.Plain(text=f'[位置: {title} - {address}\n坐标: {latitude}, {longitude}]')
            )

        elif msgtype == 'mixed':
            # 混合消息（图文消息）
            mixed_info = decrypted_msg.get('mixed', {})
            items = mixed_info.get('item', [])
            for item in items:
                item_type = item.get('type', '')
                if item_type == 'text':
                    content = item.get('content', '')
                    if content:
                        yiri_msg_list.append(platform_message.Plain(text=content))
                elif item_type == 'image':
                    sdkfileid = item.get('sdkfileid', '')
                    if sdkfileid and media_helper:
                        image_bytes = await media_helper.download_media(sdkfileid)
                        if image_bytes:
                            image_base64 = base64.b64encode(image_bytes).decode()
                            yiri_msg_list.append(
                                platform_message.Image(base64=f'data:image/png;base64,{image_base64}')
                            )
                        else:
                            yiri_msg_list.append(platform_message.Plain(text=f'[图片下载失败]'))
                    else:
                        yiri_msg_list.append(platform_message.Plain(text=f'[图片]'))

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


class WeWorkFinanceMediaAdapter(WeWorkFinanceAdapter):
    """
    企业微信会话存档适配器 - 媒体文件增强版

    在基础版本上增加完整的媒体文件下载支持。

    配置项（除了基础配置外）：
        enable_media_download: 是否启用媒体下载，默认 True
        media_download_timeout: 媒体下载超时时间（秒），默认 30
        auto_clear_cache_interval: 自动清空缓存间隔（秒），0 表示不自动清空，默认 3600（1小时）
    """

    media_helper: Optional[MediaDownloadHelper] = None

    def __init__(self, config: dict, logger):
        super().__init__(config, logger)

        # 初始化媒体下载辅助
        enable_media_download = config.get('enable_media_download', True)
        if enable_media_download:
            self.media_helper = MediaDownloadHelper(self.sdk, logger)
            logger.info('媒体文件下载功能已启用')
        else:
            logger.info('媒体文件下载功能已禁用')

        # 自动清空缓存任务
        self._cache_clear_interval = config.get('auto_clear_cache_interval', 3600)

    async def _decrypt_message(self, msg: dict) -> Optional[dict]:
        """重写解密方法，使用增强的消息转换器"""
        # 调用父类的解密逻辑
        decrypted_msg = await super()._decrypt_message(msg)
        if not decrypted_msg:
            return None

        # 如果启用了媒体下载，这里可以提前下载（可选）
        # 或者在转换时按需下载（当前实现）

        return decrypted_msg

    async def _convert_to_message_chain(self, decrypted_msg: dict) -> platform_message.MessageChain:
        """将解密后的消息转换为 MessageChain（支持媒体下载）"""
        return await WeWorkFinanceMediaMessageConverter.target2yiri(decrypted_msg, self.media_helper)

    async def _polling_loop(self):
        """后台轮询任务（增加缓存清理和消息过滤）"""
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

        last_cache_clear = asyncio.get_event_loop().time()

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
                    import json
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

                        # 转换为 MessageChain（支持媒体下载）
                        message_chain = await self._convert_to_message_chain(decrypted_msg)

                        # 转换为事件（使用标准转换器）
                        event = await WeWorkFinanceEventConverter.target2yiri(decrypted_msg)
                        if not event:
                            continue

                        # 替换消息链为增强版本
                        event.message_chain = message_chain

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

                # 定期清空缓存
                if self._cache_clear_interval > 0 and self.media_helper:
                    current_time = asyncio.get_event_loop().time()
                    if current_time - last_cache_clear >= self._cache_clear_interval:
                        cleared = self.media_helper.clear_cache()
                        if cleared > 0:
                            await self.logger.info(f'已清空 {cleared} 个媒体文件缓存')
                        last_cache_clear = current_time

                # 等待下一次轮询
                await asyncio.sleep(polling_interval)

            except asyncio.CancelledError:
                await self.logger.info('轮询任务被取消')
                break
            except Exception as e:
                import traceback
                await self.logger.error(f'轮询错误: {e}\n{traceback.format_exc()}')
                await asyncio.sleep(10)

        await self.logger.info('轮询任务结束')
