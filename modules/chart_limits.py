from modules.plans import plan_limits


def saved_chart_limit(user):
    return plan_limits(user).max_saved_charts
