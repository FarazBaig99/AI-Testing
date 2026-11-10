from .base import Connector, ConnectorError, guess_kind
from .clickup import ClickUpConnector
from .demo import DemoConnector
from .jira import JiraConnector


class _Later(Connector):
    """Tools planned for Phase 2: listed on the integration page, not connectable yet."""
    phase = 2


class AzureConnector(_Later):
    tool, label, pick_label = 'azure', 'Azure DevOps', 'Organization › Project › Team'
    about = 'Iterations → Sprints, work item types Epic / User Story / Task / Bug.'


class LinearConnector(_Later):
    tool, label, pick_label = 'linear', 'Linear', 'Team'
    about = 'Cycles → Sprints, Linear projects → Releases, labels → Bug / Story.'


class MondayConnector(_Later):
    tool, label, pick_label = 'monday', 'Monday.com', 'Board'
    about = 'Groups → Sprints, status / type columns → Story / Bug.'


CONNECTORS = {c.tool: c for c in [JiraConnector, ClickUpConnector, AzureConnector, LinearConnector,
                                  MondayConnector, DemoConnector]}


def connector_for(connection, http=None):
    return CONNECTORS[connection.tool](connection, http=http)
