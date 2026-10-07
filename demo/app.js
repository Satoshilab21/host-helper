"use strict";

(() => {
  const data = window.HOST_HELPER_DEMO;
  if (!data?.scenarios?.length) {
    document.getElementById("load-error").hidden = false;
    return;
  }

  const $ = (id) => document.getElementById(id);
  const element = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const parseDay = (value) => new Date(`${value}T12:00:00Z`);
  const dateLabel = (value, options) => parseDay(value).toLocaleDateString("en-US", { ...options, timeZone: "UTC" });
  const shortDate = (value) => dateLabel(value, { month: "short", day: "numeric" });
  const isoDay = (day) => day.toISOString().slice(0, 10);
  const stayOnDay = (stay, day) => stay.start <= day && day < stay.end;
  const days = [];
  // A fixed sample month keeps the public demo useful in any year.
  for (let day = new Date("2026-09-27T12:00:00Z"); day < new Date("2026-11-01T12:00:00Z"); day.setUTCDate(day.getUTCDate() + 1)) {
    days.push(isoDay(day));
  }

  let scenario = data.scenarios[0];
  let selectedDay = null;
  let expanded = false;

  const buttons = data.scenarios.map((item, index) => {
    const button = element("button", "scenario-button");
    button.type = "button";
    button.dataset.scenario = item.id;
    button.append(element("span", "scenario-number", String(index + 1).padStart(2, "0")), element("span", "", item.label));
    button.addEventListener("click", () => {
      scenario = item;
      selectedDay = null;
      expanded = false;
      render();
      $("announcement").textContent = item.announcement;
    });
    $("scenario-buttons").append(button);
    return button;
  });

  function renderCalendar() {
    const fragment = document.createDocumentFragment();
    for (const day of days) {
      const inMonth = day.startsWith(data.month);
      const stays = scenario.stays.filter((stay) => stayOnDay(stay, day));
      const reminders = scenario.reminders.filter((reminder) => reminder.date === day);
      const cell = element(inMonth ? "button" : "div", "day");
      if (!inMonth) cell.classList.add("outside-month");
      if (day === data.today) cell.classList.add("reference-day");
      cell.append(element("span", "day-number", String(parseDay(day).getUTCDate())));
      if (inMonth) {
        cell.type = "button";
        cell.dataset.date = day;
        cell.setAttribute("aria-pressed", String(selectedDay === day));
        cell.setAttribute("aria-label", [dateLabel(day, { month: "long", day: "numeric", weekday: "long" }), ...stays.map((stay) => stay.title), `${reminders.length} reminders`].join(". "));
        for (const stay of stays) {
          const bookingLabel = element("span", `stay-label ${stay.kind}`, stay.title);
          bookingLabel.title = stay.title;
          cell.append(bookingLabel);
        }
        if (reminders.length) cell.append(element("span", "day-reminders", `${reminders.length} reminder${reminders.length === 1 ? "" : "s"}`));
        cell.addEventListener("click", () => {
          selectedDay = day;
          renderCalendar();
          renderReminders();
          // Preserve keyboard focus after replacing the calendar's contents.
          $("calendar").querySelector(`[data-date="${day}"]`).focus({ preventScroll: true });
          $("announcement").textContent = `${shortDate(day)} selected. ${reminders.length} reminders.`;
        });
      } else {
        cell.setAttribute("aria-hidden", "true");
      }
      fragment.append(cell);
    }
    $("calendar").replaceChildren(fragment);
    $("stay-count").textContent = scenario.stays.filter((stay) => stay.kind === "booking").length;
    $("reminder-count").textContent = scenario.reminders.length;
    $("duplicate-count").textContent = scenario.duplicates;
  }

  function renderReminders() {
    const detailStage = ["standard", "enriched"].includes(scenario.id);
    $("reminder-title").textContent = selectedDay ? dateLabel(selectedDay, { weekday: "short", month: "short", day: "numeric" }) : detailStage ? "Booking details" : "Upcoming reminders";
    $("show-all").hidden = !selectedDay;
    const dayStays = selectedDay ? scenario.stays.filter((stay) => stayOnDay(stay, selectedDay) || stay.end === selectedDay) : detailStage ? scenario.stays : [];
    $("day-stays").hidden = dayStays.length === 0;
    $("day-stays").replaceChildren(...dayStays.map((stay) => {
      const card = element("div", `booking-detail ${stay.kind}`);
      card.append(element("h3", "booking-detail-title", stay.title));
      card.append(element("p", "", `${shortDate(stay.start)}–${shortDate(stay.end)} · ${stay.details.nights} nights`));
      if (stay.details.guests) card.append(element("p", "", `${stay.details.guests} guests · Check-in ${stay.details.checkIn}`));
      if (stay.details.checkOut) card.append(element("p", "", `Checkout ${stay.details.checkOut}`));
      if (stay.details.confirmation) card.append(element("p", "booking-code", `Confirmation ${stay.details.confirmation}`));
      if (stay.kind === "family") card.append(element("p", "", "Dates blocked by the host"));
      return card;
    }));
    const reminders = selectedDay ? scenario.reminders.filter((reminder) => reminder.date === selectedDay) : detailStage ? [] : scenario.reminders;
    const visible = selectedDay || expanded ? reminders : reminders.slice(0, 6);
    const fragment = document.createDocumentFragment();
    let lastDate = null;
    for (const reminder of visible) {
      if (!selectedDay && reminder.date !== lastDate) {
        fragment.append(element("h3", "reminder-date", dateLabel(reminder.date, { weekday: "short", month: "short", day: "numeric" })));
        lastDate = reminder.date;
      }
      const item = element("div", "reminder-item");
      const icon = element("span", `reminder-icon ${reminder.kind}`, reminder.kind === "trash" ? "↗" : "✓");
      icon.setAttribute("aria-hidden", "true");
      const content = element("div");
      content.append(element("h3", "", reminder.title));
      content.append(element("p", "", reminder.kind === "trash" ? `${reminder.time} · Calendar reminder` : "Task reminder"));
      item.append(icon, content);
      fragment.append(item);
    }
    if (detailStage && !selectedDay) {
      fragment.append(element("p", "empty-day", scenario.id === "standard" ? "Choose Enrich to turn these reserved dates into a useful schedule." : "Select a calendar date to see its reminders."));
    } else if (!visible.length) {
      fragment.append(element("p", "empty-day", scenario.id === "standard" ? "The standard calendar has no generated reminders yet." : "No reminders for this day. A little breathing room."));
    }
    $("reminder-list").replaceChildren(fragment);
    $("expand-reminders").hidden = Boolean(selectedDay) || reminders.length <= 6;
    $("expand-reminders").textContent = expanded ? "Show fewer reminders" : `View all ${reminders.length} reminders`;
  }

  function render() {
    for (const button of buttons) button.setAttribute("aria-pressed", String(button.dataset.scenario === scenario.id));
    $("scenario-title").textContent = scenario.title;
    $("scenario-description").textContent = scenario.description;
    const changeCount = Object.values(scenario.changes).reduce((sum, changes) => sum + changes.length, 0);
    $("change-count").textContent = scenario.id === "standard" ? "Starting point" : scenario.id === "enriched" ? "4 entries enriched" : `${changeCount} schedule changes`;
    $("booking-legend").textContent = scenario.id === "standard" ? "Reserved" : "Guest stay";
    $("family-legend").hidden = scenario.id === "standard";
    $("stay-count-label").textContent = scenario.id === "standard" ? "reserved dates" : "guest stays";
    $("reminder-footnote").textContent = scenario.id === "standard" ? "Enrichment uses sample confirmation emails to identify guests and host-blocked dates." : "Trash goes on the calendar. Cleaning and upkeep become task reminders.";
    renderCalendar();
    renderReminders();
  }

  $("show-all").addEventListener("click", () => {
    selectedDay = null;
    renderCalendar();
    renderReminders();
    $("announcement").textContent = "Showing upcoming reminders for the month.";
    const focusTarget = $("expand-reminders").hidden ? buttons.find((button) => button.dataset.scenario === scenario.id) : $("expand-reminders");
    focusTarget.focus({ preventScroll: true });
  });
  $("expand-reminders").addEventListener("click", () => {
    expanded = !expanded;
    renderReminders();
    $("announcement").textContent = expanded ? "Showing all reminders." : "Showing the next six reminders.";
  });
  render();
})();
