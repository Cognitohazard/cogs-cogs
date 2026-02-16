from .remindconfirm import RemindConfirm


async def setup(bot):
    cog = RemindConfirm(bot)
    await bot.add_cog(cog)
