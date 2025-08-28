from apscheduler.schedulers.asyncio import AsyncIOScheduler
from service.socket_manager import socket_manager, memory
from service.capital_api import update_auth_header, get_epic_hours, update_markets


class Jobs:
    
    async def update_epic_hours(self):
        for epic in socket_manager.get_all_subscribed_epics():
            memory.trading_hours[epic] = await get_epic_hours(epic)
        print(f"EPIC_HOURS_UPDATED => ", len(socket_manager.get_all_subscribed_epics()))

    async def run(self):
        scheduler = AsyncIOScheduler()
        
        # ping socket
        scheduler.add_job(socket_manager.ping_all, "interval", minutes=5)
        # update auth header
        scheduler.add_job(update_auth_header, "interval", minutes=5)
        # update markets
        scheduler.add_job(update_markets, "interval", hours=5)
        # update epic hours
        scheduler.add_job(self.update_epic_hours, "interval", hours=5)

        # start schd
        scheduler.start()
        for job in scheduler.get_jobs():
            print("Next Job: ", job.next_run_time)
            
            


jobs = Jobs()