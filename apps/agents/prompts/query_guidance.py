"""Shared semantic date-window instructions for artifact agents."""

RECENT_PERIOD_QUERY_GUIDANCE = """- For "most recent 24 weeks, oldest first", use a query like
  `{"measures":["visits.count"],"time_dimension":"visits.visit_date","granularity":"week",
  "date_range":{"last":24,"unit":"week"},"order_by":[{"field":"visits.visit_date","direction":"asc"}],"limit":500}`.
  Counted windows support day/week/month/quarter/year, including the current calendar
  period through today in Scout's reporting timezone; weeks start Monday. They are
  anchored to today, not the latest data row, and preserve the requested window even
  when periods have no data (they do not generate zero-filled rows). For completed
  periods or a data-relative anchor, use explicit start/end dates. A bound date control
  or comparison overrides query-local date_range. `limit` caps rows, not periods:
  allow for all series and check truncation; never use limit=N to choose N periods.
"""
