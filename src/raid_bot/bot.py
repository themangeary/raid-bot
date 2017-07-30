import asyncio
import discord
import functools
import json
import re
import sys
import time
import pytz
from collections import namedtuple
from datetime import datetime, timedelta, timezone


def cached_attribute(func):
    """decorator that returns a cached results."""

    @functools.wraps(func)
    def inner_func(server):
        key = func.__name__
        record = cache.setdefault(server, dict())
        if key in record:
            return record[key]
        return record.setdefault(key, func(server))
    return inner_func

# process level client
client = discord.Client()

# process level cache by server
# discord.Server -> dict()
cache = dict()

# bot specific settings
settings = None

#
# Bot Cacheable Attributes
#
# If problem with this crops up, removing the cached_attribute decorator will remove caching behavior.
#
# Some attributes of the bot are fixed when the bot starts, like:
#
#   - The announcement channel where the bot looks and posts new raids
#   - The backup channel where the bot suggests going if raid channels are full
#   - The raid channels, which are looked for the first time and never rescanned
#


@cached_attribute
def get_raid_additional_roles(server):
    """Gets roles that should get read perms when the raid is started."""
    return [r for r in server.roles if r.name in settings.raid_additional_roles]


@cached_attribute
def get_raid_channels(server):
    """Gets the list of raid channels for ther server."""
    raid_channels = []
    rx = re.compile(settings.raid_channel_regex)
    for channel in server.channels:
        p = channel.permissions_for(server.me)
        if rx.search(channel.name) and p.manage_roles and p.manage_messages and p.manage_channels and p.read_messages:
            raid_channels.append(channel)
    return raid_channels


@cached_attribute
def get_announcement_channel(server):
    """Gets the announcement channel for a server."""
    return discord.utils.find(lambda c: c.name == settings.announcement_channel, server.channels)


@cached_attribute
def get_backup_channel(server):
    """Gets the announcement channel for a server."""
    return discord.utils.find(lambda c: c.name == settings.backup_raid_channel, server.channels)


#
# End server cacheable attributes
#


#
# Configuration abstraction
#

def get_join_emoji():
    """Gets the join emoji for a server."""
    return settings.raid_join_emoji


def get_leave_emoji():
    """Gets the leave emoji for a server."""
    return settings.raid_leave_emoji


def get_full_emoji():
    """Gets the full emoji for a server."""
    return settings.raid_full_emoji


def strfdelta(tdelta, fmt):
    d = {"days": tdelta.days}
    d["hours"], rem = divmod(tdelta.seconds, 3600)
    d["minutes"], d["seconds"] = divmod(rem, 60)
    return fmt.format(**d)


def adjusted_datetime(dt, tz='US/Eastern'):
    zone = pytz.timezone(tz)
    return dt + zone.utcoffset(dt)


def get_raid_expiration(started_dt):
    return started_dt + timedelta(seconds=settings.raid_duration_seconds)

#
#
#

#
# Raid properties
#


def get_raid_members(channel):
    return [target for target, _ in channel.overwrites if isinstance(target, discord.User)]


def get_raid_start_embed(creator, started_dt, expiration_dt):
    embed = discord.Embed()
    embed.color = discord.Color.green()
    embed.title = 'A raid has started!'
    embed.add_field(name='creator', value=creator.mention, inline=True)
    embed.add_field(name='started at', value=started_dt.strftime(settings.time_format), inline=False)
    embed.add_field(name='channel expires', value=expiration_dt.strftime(settings.time_format), inline=False)
    embed.set_footer(text='To join, tap {} below'.format(get_join_emoji()))
    return embed


def get_raid_end_embed(creator, started_dt, ended_dt):
    duration = ended_dt - started_dt
    embed = discord.Embed()
    embed.color = discord.Color.red()
    embed.title = 'This raid has ended.'
    embed.add_field(name='creator', value=creator.mention if creator else None, inline=True)
    embed.add_field(name='duration', value=strfdelta(duration, '{hours:02}:{minutes:02}:{seconds:02}'), inline=True)
    embed.add_field(name='started at', value=started_dt.strftime(settings.time_format), inline=False)
    embed.add_field(name='ended at', value=ended_dt.strftime(settings.time_format), inline=False)
    return embed


def get_success_embed(text):
    embed = discord.Embed()
    embed.color = discord.Color.green()
    embed.description = text
    return embed


def get_error_embed(text):
    embed = discord.Embed()
    embed.color = discord.Color.red()
    embed.description = text
    return embed


def get_raid_busy_embed(channel):
    embed = discord.Embed()
    embed.color = discord.Color.dark_teal()
    embed.title = 'All raid channels are busy at the moment.'
    embed.description = 'Coordinate this raid in {} instead. More channels will be available later.'.format(channel.mention)
    return embed


def get_raid_members_embed(members):
    embed = discord.Embed()
    embed.title = "Raid Members ({})".format(len(members))
    embed.description = "\n".join(sorted(member.display_name for member in members))
    embed.color = discord.Color.green()
    return embed


def get_raid_summary_embed(creator, expiration_dt, text):
    embed = discord.Embed()
    embed.title = 'Welcome to this raid channel!'
    embed.description = "**{}**".format(text)
    embed.add_field(name='creator', value=creator.mention)
    embed.add_field(name='channel expires', value=expiration_dt.strftime(settings.time_format))
    embed.add_field(name="commands", value="You can use the following commands:", inline=False)
    embed.add_field(name="$leaveraid", value="Removes you from this raid.", inline=False)
    embed.add_field(name="$listraid", value="Shows all current members of this raid channel.", inline=False)
    embed.add_field(name="$endraid", value="Ends the raid and closes the channel.", inline=False)
    embed.set_footer(text='You can also leave the raid with the {} reaction below.'.format(get_leave_emoji()))
    embed.color = discord.Color.green()
    return embed


def is_raid_start_message(message):
    """Whether this is the start of a new raid."""
    if message.role_mentions:
        return any(mention.name.startswith('raid-') for mention in message.role_mentions)


def is_announcement_channel(channel):
    """Whether the channel is the announcement channel."""
    return channel and channel == get_announcement_channel(channel.server)


def is_raid_channel(channel):
    """Whether the channel is a valid raid_channel.
    """
    return channel and channel in get_raid_channels(channel.server)



def is_open(channel):
    return channel.topic is None


async def get_announcement_message(raid_channel):
    """Gets the message that created this channel."""
    server = raid_channel.server
    announcement_channel = get_announcement_channel(server)
    message_id = raid_channel.topic
    try:
        message = await client.get_message(announcement_channel, message_id)
    except:
        return None  # an error occurred, return None TODO: log here
    return message


async def get_raid_creator(raid_channel):
    message = await get_announcement_message(raid_channel)
    if message and message.embeds:
        embed = message.embeds[0]
        fields = embed.get('fields', [])
        if fields:
            creator_mention = fields[0]['value']
            for target, _ in raid_channel.overwrites:
                if isinstance(target, discord.User) and target.mention == creator_mention:
                    return target


def get_raid_channel(message):
    """Pulls out the channel field from the message embed."""
    return message.channel_mentions[0] if message.channel_mentions else None


async def is_raid_expired(raid_channel):
    message = await get_announcement_message(raid_channel)
    if message is None:
        return True  # can't find message, clean up the raid channel
    create_ts = message.timestamp.replace(tzinfo=timezone.utc).timestamp()
    return settings.raid_duration_seconds < time.time() - create_ts


def get_available_raid_channel(server):
    """We may need to wrap function calls to this in a lock."""
    for channel in get_raid_channels(server):
        if is_open(channel):
            return channel




async def start_raid_group(user, message_id, description):
    # get the server
    server = user.server

    # find an available raid channel
    channel = get_available_raid_channel(server)

    if channel:
        # set the topic
        await client.edit_channel(channel, topic=message_id)

        # get the message
        announcement_channel = get_announcement_channel(server)
        message = await client.get_message(announcement_channel, message_id)

        # calculate expiration time
        expiration_dt = adjusted_datetime(get_raid_expiration(message.timestamp))
        summary_message = await client.send_message(channel, embed=get_raid_summary_embed(user, expiration_dt, description))

        # add shortcut reactions for commands
        await client.add_reaction(summary_message, get_leave_emoji())

        # set channel permissions to make raid viewers see the raid.
        for role in get_raid_additional_roles(server):
            perms = discord.PermissionOverwrite(read_messages=True)
            await client.edit_channel_permissions(channel, role, perms)

        return channel

async def end_raid_group(channel):
    # get the creator before we remove roles
    creator = await get_raid_creator(channel)

    # remove all the permissions
    raid_viewer_roles = get_raid_additional_roles(channel.server)
    for target, _ in channel.overwrites:
        if isinstance(target, discord.User) or target in raid_viewer_roles:
            await client.delete_channel_permissions(channel, target)

    # purge all messages
    await client.purge_from(channel)

    # update the message if its available
    message = await get_announcement_message(channel)
    if message:
        started_dt = adjusted_datetime(message.timestamp)
        ended_dt = datetime.now()
        await client.edit_message(message, embed=get_raid_end_embed(creator, started_dt, ended_dt))
        await client.clear_reactions(message)

    # remove the topic
    channel = await client.edit_channel(channel, topic=None)


async def invite_user_to_raid(channel, user):
    # adds an overwrite for the user
    perms = discord.PermissionOverwrite(read_messages=True)
    await client.edit_channel_permissions(channel, user, perms)

    # sends a message to the raid channel the user was added
    await client.send_message(channel,
                              "{}, you are now a member of this raid group.".format(user.mention),
                              embed=get_success_embed('{} has joined the raid!'.format(user.mention)))


async def uninvite_user_from_raid(channel, user):
    # reflect the proper number of members (the bot role and everyone are excluded)
    await client.delete_channel_permissions(channel, user)
    await client.send_message(channel, embed=get_error_embed('{} has the left raid!'.format(user.mention)))

    # remove the messages emoji
    server = channel.server
    announcement_message = await get_announcement_message(channel)
    await client.remove_reaction(announcement_message, get_join_emoji(), user)


async def list_raid_members(channel):
    members = get_raid_members(channel)
    await client.send_message(channel, embed=get_raid_members_embed(members))


async def cleanup_raid_channels():
    await client.wait_until_ready()
    while not client.is_closed:
        for server in client.servers:
            announcement_channel = get_announcement_channel(server)
            channels = get_raid_channels(server)
            for channel in channels:
                if not is_open(channel):
                    expired = await is_raid_expired(channel)
                    if expired or not get_raid_members(channel):
                        await end_raid_group(channel)

        await asyncio.sleep(settings.raid_cleanup_interval_seconds)


@client.event
async def on_ready():
    print('Logged in as {}'.format(client.user.name))
    print('------')


    for server in client.servers:
        print('server: {}'.format(server.name))
        print('announcement channel: {}'.format(get_announcement_channel(server).name))
        print('backup channel: {}'.format(get_backup_channel(server).name))

        raid_channels = get_raid_channels(server)
        print('{} raid channel(s)'.format(len(raid_channels)))
        for channel in raid_channels:
            print('raid channel: {}'.format(channel.name))


@client.event
async def on_reaction_add(reaction, user):
    """Invites a user to a raid channel they react to they are no already there."""
    server = reaction.message.server
    message = reaction.message
    if user == server.me:
        return

    announcement_channel = get_announcement_channel(server)
    if reaction.emoji == get_join_emoji() and announcement_channel == reaction.message.channel:
        raid_channel = get_raid_channel(message)
        announcement_message = await get_announcement_message(raid_channel)
        if is_raid_channel(raid_channel) and announcement_message:
            # NB: use overwrites for, since admins otherwise won't be notified
            # we know the channel is private and only overwrites matter
            if raid_channel.overwrites_for(user).is_empty():
                await invite_user_to_raid(raid_channel, user)

    elif reaction.emoji == get_leave_emoji():
        raid_channel = message.channel
        if is_raid_channel(raid_channel) and reaction.message.author == server.me:
            # NB: use overwrites for, since admins otherwise won't be notified
            # we know the channel is private and only overwrites matter
            if not raid_channel.overwrites_for(user).is_empty():
                await uninvite_user_from_raid(raid_channel, user)

                # remove this reaction
                await client.remove_reaction(message, reaction.emoji, user)


@client.event
async def on_reaction_remove(reaction, user):
    """Uninvites a user to a raid when they remove a reaction if they are there."""
    server = reaction.message.server
    if user == server.me:
        return

    announcement_channel = get_announcement_channel(server)
    if reaction.emoji == get_join_emoji() and announcement_channel == reaction.message.channel:
        message = reaction.message
        raid_channel = get_raid_channel(message)
        announcement_message = await get_announcement_message(raid_channel)
        if is_raid_channel(raid_channel) and announcement_message:
            # NB: use overwrites for, since admins otherwise won't be notified
            # we know the channel is private and only overwrites matter
            if not raid_channel.overwrites_for(user).is_empty():
                await uninvite_user_from_raid(raid_channel, user)

@client.event
async def on_message(message):
    # we'll need this for future
    server = message.server
    channel = message.channel
    user = message.author
    if user == server.me:
        return

    if is_announcement_channel(channel) and is_raid_start_message(message):
        # send the message, then edit the raid to avoid a double notification
        raid_message = await client.send_message(channel, "Looking for open channels...")
        raid_channel = await start_raid_group(user, raid_message.id, message.clean_content)
        if raid_channel:
            started_dt = adjusted_datetime(raid_message.timestamp)
            expiration_dt = adjusted_datetime(get_raid_expiration(raid_message.timestamp))
            raid_message = await client.edit_message(raid_message,
                                                     '**{}**\n\n**in:** {}'.format(message.content, raid_channel.mention),
                                                     embed=get_raid_start_embed(user, started_dt, expiration_dt))

            # invite the member
            await invite_user_to_raid(raid_channel, user)

            # add a join reaction to the message
            join_emoji = get_join_emoji()
            await client.add_reaction(raid_message, join_emoji)
        else:
            # notify them to use the backup raid channel, this won't be monitored
            backup_channel = get_backup_channel(server)

            m = await client.edit_message(raid_message,
                                          '*"{}"*\n\n**in:** {}'.format(message.content, backup_channel.mention),
                                          embed=get_raid_busy_embed(backup_channel))
            await client.add_reaction(m, get_full_emoji())
    elif is_raid_channel(channel) and message.content.startswith('$leaveraid'):
        await uninvite_user_from_raid(channel, user)
    elif is_raid_channel(channel) and message.content.startswith('$listraid'):
        await list_raid_members(channel)
    elif is_raid_channel(channel) and message.content.startswith('$endraid'):
        creator = await get_raid_creator(channel)
        if creator == user:
            await end_raid_group(channel)
        else:
            await client.send_message(channel, embed=get_error_embed('Only the creator may end the raid.'))


def get_args():
    from argparse import ArgumentParser
    parser = ArgumentParser(description="Pokemon Go discord bot for coordinating raids.")
    parser.add_argument("--token", required=True, default=None, help="The token to use when running the bot.")
    parser.add_argument("--announcement-channel", required=True, default=None,
                        help="Channel to listen for and announce raids on (default: %(default)s)")
    parser.add_argument("--backup-raid-channel", default="raid-coordination",
                        help="The channel to use when raid channels are unavailable (default: %(default)s)")
    parser.add_argument("--raid-channel-regex", default="^raid-group-.+",
                        help="Pattern which all raid channels must have. (default: %(default)s)")
    parser.add_argument("--raid-start-regex", default="^raid-.+",
                        help="Regex for role mentions to trigger a raid. (default: %(default)s)")
    parser.add_argument("--raid-duration-seconds", type=int, default=7200,
                        help="Time until a raid group expires, in seconds (default: %(default)s).")
    parser.add_argument("--raid-cleanup-interval-seconds", type=int, default=60,
                        help="Time between checks for cleaning up raids (default: %(default)s)")
    parser.add_argument("--raid-additional-roles", default=[], action='append',
                        help="Additional roles to permission on active raid channels (default: %(default)s)")
    parser.add_argument("--raid-join-emoji", default='\U0001F464', help="Emoji used for joining raids (default: %(default)s)")
    parser.add_argument("--raid-leave-emoji", default='\U0001F6AA', help="Emoji used for leaving raids (default: %(default)s)")
    parser.add_argument("--raid-full-emoji", default='\U0001F61F', help="Emoji used for full raid channels (default: %(default)s)")
    parser.add_argument("--time-format", default='%Y-%m-%d %I:%M:%S %p', help="The time format to use. (default: %(default)s)")
    args = parser.parse_args()
    return args


def main():
    global settings
    settings = get_args()
    client.loop.create_task(cleanup_raid_channels())
    client.run(settings.token)

