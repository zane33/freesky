import reflex as rx
from typing import Dict, List, TypedDict
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta
from dateutil import parser
from freesky import backend
from freesky.components import navbar
from freesky.auth_state import require_login


class ChannelItem(TypedDict):
    name: str
    id: str


class EventItem(TypedDict):
    name: str
    time: str
    dt: datetime
    category: str
    channels: List[ChannelItem]


class ScheduleState(rx.State):
    events: List[EventItem] = []
    categories: Dict[str, bool] = {}
    switch: bool = True
    search_query: str = ""
    # ISO dates (YYYY-MM-DD) from the native date inputs; "" = unbounded.
    date_from: str = ""
    date_to: str = ""
    # Distinguishes "still fetching" from "fetched, nothing there". Without it an
    # empty schedule rendered the loading spinner forever, which looked broken.
    loaded: bool = False

    @staticmethod
    def get_channels(channels: dict) -> List[ChannelItem]:
        channel_list = []
        if isinstance(channels, list):
            for channel in channels:
                try:
                    channel_list.append(ChannelItem(name=channel["channel_name"], id=channel["channel_id"]))
                except:
                    continue
        elif isinstance(channels, dict):
            for channel_dic in channels:
                try:
                    channel_list.append(ChannelItem(name=channels[channel_dic]["channel_name"], id=channels[channel_dic]["channel_id"]))
                except:
                    continue
        return channel_list

    def toggle_category(self, category):
        self.categories[category] = not self.categories.get(category, False)

    @rx.event
    def set_all_categories(self, on: bool):
        self.categories = {c: on for c in self.categories}

    def double_category(self, category):
        for cat in self.categories:
            if cat != category:
                self.categories[cat] = False
            else:
                self.categories[cat] = True

    async def on_load(self):
        redirect = await require_login(self)
        if redirect is not None:
            return redirect
        self.events = []
        categories = {}
        try:
            # Same process as the backend, so call it directly. The old code
            # httpx-GET'd the relative path "/schedule", which can't resolve
            # without a base URL — it always raised and silently fell through.
            days = backend._filter_schedule_to_enabled(await backend.get_schedule() or {})

            for day in days:
                name = day.split(" - ")[0]
                dt = parser.parse(name, dayfirst=True)
                for category in days[day]:
                    categories[category] = True
                    for event in days[day][category]:
                        time = event["time"]
                        hour, minute = map(int, time.split(":"))
                        # Upstream labels the day "Schedule Time UK GMT" but the
                        # times are wall-clock London (BST in summer), not UTC.
                        event_dt = dt.replace(hour=hour, minute=minute, tzinfo=ZoneInfo("Europe/London"))
                        channels = self.get_channels(event.get("channels"))
                        channels.extend(self.get_channels(event.get("channels2")))
                        channels.sort(key=lambda channel: channel["name"])
                        self.events.append(EventItem(name=event["event"], time=time, dt=event_dt, category=category, channels=channels))
        except Exception as e:
            # ponytail: no invented events. This used to fabricate 24 hours of
            # "Sports Event N"/"News Hour N" on ESPN/CNN/HBO, which rendered as a
            # real schedule and hid the fact that upstream returned nothing.
            print(f"Schedule loading error: {str(e)}")
            categories = {}

        self.categories = dict(sorted(categories.items()))
        self.events.sort(key=lambda event: event["dt"])
        self.loaded = True

    @rx.event
    def set_switch(self, value: bool):
        self.switch = value

    # reflex 0.9 no longer auto-generates set_* handlers (state_auto_setters=False)
    @rx.event
    def set_search_query(self, value: str):
        self.search_query = value

    @rx.event
    def set_date_from(self, value: str):
        self.date_from = value

    @rx.event
    def set_date_to(self, value: str):
        self.date_to = value

    @rx.var
    def filtered_events(self) -> List[EventItem]:
        now = datetime.now(ZoneInfo("UTC")) - timedelta(minutes=30)
        query = self.search_query.strip().lower()

        def in_range(dt: datetime) -> bool:
            # ponytail: compared on the London calendar day the listing uses,
            # not the viewer's local day; an event near midnight can land a day
            # off for far-away zones. Ship the browser tz if that bites.
            day = dt.astimezone(ZoneInfo("Europe/London")).date().isoformat()
            return (not self.date_from or day >= self.date_from) and (not self.date_to or day <= self.date_to)

        return [
            event for event in self.events
            if self.categories.get(event["category"], False)
               and (not self.switch or event["dt"] > now)
               and in_range(event["dt"])
               and (query == "" or query in event["name"].lower())
        ]

    @rx.var
    def shown_count(self) -> str:
        """Say WHY events are hidden. "6 of 77" alone read as a bug when the
        missing 71 had simply already kicked off."""
        shown = len(self.filtered_events)
        cutoff = datetime.now(ZoneInfo("UTC")) - timedelta(minutes=30)
        past = sum(1 for e in self.events if e["dt"] <= cutoff) if self.switch else 0
        note = f" · {past} already started (hidden)" if past else ""
        return f"{shown} of {len(self.events)} events{note}"


def event_card(event: EventItem) -> rx.Component:
    return rx.card(
        rx.heading(event["name"]),
        rx.hstack(
            rx.moment(event["dt"], format="HH:mm", local=True),
            rx.moment(event["dt"], format="ddd MMM DD YYYY", local=True),
            rx.badge(event["category"], margin_top="0.2rem"),
        ),
        rx.hstack(
            rx.foreach(
                event["channels"],
                lambda channel: rx.button(channel["name"], variant="surface", color_scheme="gray", size="1", on_click=rx.redirect(f"/watch/{channel['id']}")),
            ),
            wrap="wrap",
            margin_top="0.5rem",
        ),
        width="100%",
    )


def category_badge(category) -> rx.Component:
    return rx.badge(
        category[0],
        color_scheme=rx.cond(
            category[1],
            "red",
            "gray",
        ),
        _hover={"color": "white"},
        style={"cursor": "pointer"},
        on_click=lambda: ScheduleState.toggle_category(category[0]),
        on_double_click=lambda: ScheduleState.double_category(category[0]),
        size="2",
    )


@rx.page("/schedule", on_load=ScheduleState.on_load)
def schedule() -> rx.Component:
    return rx.box(
        navbar(),
        rx.container(
            rx.center(
                rx.vstack(
                    rx.cond(
                        ScheduleState.categories,
                        rx.card(
                            rx.input(
                                placeholder="Search events...",
                                on_change=ScheduleState.set_search_query,
                                value=ScheduleState.search_query,
                                width="100%",
                                size="3",
                            ),
                            rx.hstack(
                                rx.text("Filter by tag:"),
                                rx.button("All", size="1", variant="soft", on_click=ScheduleState.set_all_categories(True)),
                                rx.button("None", size="1", variant="soft", on_click=ScheduleState.set_all_categories(False)),
                                rx.foreach(ScheduleState.categories, category_badge),
                                spacing="2",
                                wrap="wrap",
                                margin_top="0.7rem",
                            ),
                            rx.hstack(
                                rx.text("Hide past events"),
                                rx.switch(
                                    on_change=ScheduleState.set_switch,
                                    checked=ScheduleState.switch,
                                    margin_top="0.2rem"
                                ),
                                rx.text("From", margin_left="1rem"),
                                rx.input(type="date", value=ScheduleState.date_from, on_change=ScheduleState.set_date_from, size="1"),
                                rx.text("To"),
                                rx.input(type="date", value=ScheduleState.date_to, on_change=ScheduleState.set_date_to, size="1"),
                                rx.text(ScheduleState.shown_count, color="gray", size="2", margin_left="auto"),
                                margin_top="0.5rem",
                                wrap="wrap",
                                align="center",
                                width="100%",
                            ),
                        ),
                        rx.cond(
                            ScheduleState.loaded,
                            rx.card(
                                rx.vstack(
                                    rx.icon("calendar-off", size=32, color="gray"),
                                    rx.heading("No schedule available", size="5"),
                                    rx.text(
                                        "The upstream site restricts its schedule API to "
                                        "approved domains, so there are no listings to show. "
                                        "Channels and playback are unaffected.",
                                        color="gray",
                                        size="2",
                                        text_align="center",
                                    ),
                                    align="center",
                                    spacing="3",
                                ),
                                padding="2rem",
                            ),
                            rx.spinner(size="3"),
                        ),
                    ),
                    rx.foreach(ScheduleState.filtered_events, event_card),
                ),
            ),
            padding_top="10rem",
        ),
    )
