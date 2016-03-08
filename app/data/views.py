import json
import logging
import uuid

from dateutil.parser import parse as parse_date
from datetime import timedelta

from celery import states

from django.conf import settings
from django.db import transaction
from django.db.models import (Case,
                              When,
                              IntegerField,
                              DateTimeField,
                              CharField,
                              UUIDField,
                              Value,
                              Count,
                              Q)
from django_redis import get_redis_connection

from rest_framework import viewsets
from rest_framework.decorators import list_route, detail_route
from rest_framework.exceptions import ParseError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.settings import api_settings
from rest_framework import renderers, status

from rest_framework_csv import renderers as csv_renderer

from ashlar.models import RecordSchema, RecordType, Record, BoundaryPolygon, Boundary
from ashlar.views import (BoundaryPolygonViewSet,
                          RecordViewSet,
                          RecordTypeViewSet,
                          RecordSchemaViewSet,
                          BoundaryViewSet)

from ashlar.serializers import RecordSerializer, RecordSchemaSerializer

from driver_auth.permissions import (IsAdminOrReadOnly,
                                     ReadersReadWritersWrite,
                                     IsAdminAndReadOnly,
                                     is_admin_or_writer)
from data.tasks import export_csv

import filters
from models import RecordAuditLogEntry, RecordDuplicate
from serializers import (DetailsReadOnlyRecordSerializer, DetailsReadOnlyRecordSchemaSerializer,
                         RecordAuditLogEntrySerializer, RecordDuplicateSerializer)
import transformers
from driver import mixins

logger = logging.getLogger(__name__)

DateTimeField.register_lookup(transformers.ISOYearTransform)
DateTimeField.register_lookup(transformers.WeekTransform)


class DriverRecordViewSet(RecordViewSet, mixins.GenerateViewsetQuery):
    """Override base RecordViewSet from ashlar to provide aggregation and tiler integration
    """
    permission_classes = (ReadersReadWritersWrite,)

    # Filter out everything except details for read-only users
    def get_serializer_class(self):
        # check if parameter details_only is set to true, and if so, use details-only serializer
        requested_details_only = False
        details_only_param = self.request.query_params.get('details_only', None)
        if details_only_param == 'True' or details_only_param == 'true':
            requested_details_only = True

        if is_admin_or_writer(self.request.user) and not requested_details_only:
            return RecordSerializer
        return DetailsReadOnlyRecordSerializer

    # Change auditing
    def add_to_audit_log(self, request, instance, action):
        """Creates a new audit log entry; instance must have an ID"""
        if not instance.pk:
            raise ValueError('Cannot create audit log entries for unsaved model objects')
        if action not in RecordAuditLogEntry.ActionTypes.as_list():
            raise ValueError("{} not one of 'create', 'update', or 'delete'".format(action))
        RecordAuditLogEntry.objects.create(user=request.user,
                                           username=request.user.username,
                                           record=instance,
                                           record_uuid=str(instance.pk),
                                           action=action)

    @transaction.atomic
    def perform_create(self, serializer):
        instance = serializer.save()
        self.add_to_audit_log(self.request, instance, RecordAuditLogEntry.ActionTypes.CREATE)

    @transaction.atomic
    def perform_update(self, serializer):
        instance = serializer.save()
        self.add_to_audit_log(self.request, instance, RecordAuditLogEntry.ActionTypes.UPDATE)

    @transaction.atomic
    def perform_destroy(self, instance):
        self.add_to_audit_log(self.request, instance, RecordAuditLogEntry.ActionTypes.DELETE)
        instance.delete()

    # Views
    def list(self, request, *args, **kwargs):
        # Don't generate a tile key unless the user specifically requests it, to avoid
        # filling up the Redis cache with queries that will never be viewed as tiles
        if ('tilekey' in request.query_params and
                request.query_params['tilekey'] in ['True', 'true']):
            response = Response(dict())
            query_sql = self.generate_query_sql(request)
            tile_token = uuid.uuid4()
            self._cache_tile_sql(tile_token, query_sql.encode('utf-8'))
            response.data['tilekey'] = tile_token
        else:
            response = super(DriverRecordViewSet, self).list(self, request, *args, **kwargs)
        return response

    @list_route(methods=['get'])
    def stepwise(self, request):
        """Return an aggregation counts the occurrence of events per week (per year) between
        two bounding datetimes
        e.g. [{"week":35,"count":13,"year":2015},{"week":43,"count":1,"year":2015}]
        """
        # We'll need to have minimum and maximum dates specified to properly construct our SQL
        try:
            start_date = parse_date(request.query_params['occurred_min'])
            end_date = parse_date(request.query_params['occurred_max'])
        except KeyError:
            raise ParseError("occurred_min and occurred_max must both be provided")
        except ValueError:
            raise ParseError("occurred_min and occurred_max must both be valid dates")

        # The min year can't be after or more than 2000 years before the max year
        year_distance = end_date.year - start_date.year
        if year_distance < 0:
            raise ParseError("occurred_min must be an earlier date than occurred_max")
        if year_distance > 2000:
            raise ParseError("occurred_min and occurred_max must be within 2000 years of one another")

        queryset = self.get_queryset()
        for backend in list(self.filter_backends):
            queryset = backend().filter_queryset(request, queryset, self)

        # Build SQL `case` statement to annotate with the year
        isoyear_case = Case(*[When(occurred_from__isoyear=year, then=Value(year))
                              for year in xrange(start_date.year, end_date.year + 1)],
                            output_field=IntegerField())
        # Build SQL `case` statement to annotate with the day of week
        week_case = Case(*[When(occurred_from__week=week, then=Value(week))
                           for week in xrange(1, 54)],
                         output_field=IntegerField())

        annotated_recs = queryset.annotate(year=isoyear_case).annotate(week=week_case)

        # Voodoo to perform aggregations over `week` and `year` combinations
        counted = (annotated_recs.values('week', 'year')
                   .order_by('week', 'year')
                   .annotate(count=Count('week')))

        return Response(counted)

    @list_route(methods=['get'])
    def toddow(self, request):
        """ Return aggregations which nicely format the counts for time of day and day of week
        e.g. [{"count":1,"dow":6,"tod":1},{"count":1,"dow":3,"tod":3}]
        """
        queryset = self.get_queryset()
        for backend in list(self.filter_backends):
            queryset = backend().filter_queryset(request, queryset, self)

        # Build SQL `case` statement to annotate with the day of week
        dow_case = Case(*[When(occurred_from__week_day=x, then=Value(x))
                          for x in xrange(1, 8)], output_field=IntegerField())
        # Build SQL `case` statement to annotate with the time of day
        tod_case = Case(*[When(occurred_from__hour=x, then=Value(x))
                          for x in xrange(24)], output_field=IntegerField())
        annotated_recs = queryset.annotate(dow=dow_case).annotate(tod=tod_case)

        # Voodoo to perform aggregations over `tod` and `dow` combinations
        counted = (annotated_recs.values('tod', 'dow')
                   .order_by('tod', 'dow')
                   .annotate(count=Count('tod')))
        return Response(counted)

    @list_route(methods=['get'])
    def crosstabs(self, request):
        """Returns a columnar aggregation of event totals; this is essentially a generalized ToDDoW

        Requires the following query parameters:
        - Exactly one row specification parameter chosen from:
            - row_period_type: A time period to use as rows; valid choices are:
                               {'hour', 'day', 'week_day', 'week', 'month', 'year'}
                               The value 'day' signifies day-of-month
            - row_boundary_id: Id of a Boundary whose BoundaryPolygons should be used as rows
            - row_choices_path: Path components to a schema property whose choices should be used
                                as rows, separated by commas
                                e.g. &row_choices_path=accidentDetails,properties,Collision%20type
                                Note that ONLY properties which have an 'enum' key are valid here.
        - Exactly one column specification parameter chosen from:
            - col_period_type
            - col_boundary_id
            - col_choices_path
            As you might expect, these operate identically to the row_* parameters above, but the
            results are used as columns instead.
        - record_type: The UUID of the record type which should be aggregated

        Allows the following query parameters:
        - aggregation_boundary: Id of a Boundary; separate tables will be generated for each
                                BoundaryPolygon associated with the Boundary.
        - all other filter params accepted by the list endpoint; these will filter the set of
            records before any aggregation is applied. This may result in some rows / columns /
            tables having zero records.

        Response format:
        {
            "row_labels": {
                "row_key1": "row_label1",
                ...
            },
            "col_labels": {
                "col_key1": "col_label1",
                ...
            },
            "table_labels": {
                "table_key1": "table_label1",
                // This will be empty if aggregation_boundary is not provided
            },
            "tables": [
                {
                    "tablekey": "table_key1",
                    "data": [
                        {
                            "row": "row_key1",
                            "col": "col_key1",
                            "count": X
                        },
                        ...
                    ]
                },
                ...
            ]
        }
        Note that all combinations of rows / columns are NOT guaranteed to exist within the
        table `data` arrays; in other words, the tables are sparse.
        """
        valid_row_params = set(['row_period_type', 'row_boundary_id', 'row_choices_path'])
        valid_col_params = set(['col_period_type', 'col_boundary_id', 'col_choices_path'])
        # Validate there's exactly one row_* and one col_* parameter
        row_params = set(request.query_params) & valid_row_params
        col_params = set(request.query_params) & valid_col_params
        if len(row_params) != 1 or len(col_params) != 1:
            raise ParseError(detail='Exactly one col_* and row_* parameter required; options are {}'
                                    .format(list(valid_row_params | valid_col_params)))

        # Pass parameters to case-statement generators
        row_param = row_params.pop()  # Guaranteed to be just one at this point
        col_param = col_params.pop()
        row_case, row_labels = self._query_param_to_case_stmnt(row_param, request)
        col_case, col_labels = self._query_param_to_case_stmnt(col_param, request)

        # Generate filtered queryset based on other params
        queryset = self.get_queryset()
        for backend in list(self.filter_backends):
            queryset = backend().filter_queryset(request, queryset, self)

        # Apply case statements to filtered queryset
        annotated_qs = queryset.annotate(row=row_case).annotate(col=col_case)

        # If aggregation_boundary_id exists, grab the associated BoundaryPolygons.
        tables_boundary = request.query_params.get('aggregation_boundary', None)
        if tables_boundary:
            boundaries = BoundaryPolygon.objects.filter(boundary=tables_boundary)
        else:
            boundaries = None

        # Assemble crosstabs either once or multiple times if there are boundaries
        response = dict(tables=[], table_labels=dict(), row_labels=row_labels,
                        col_labels=col_labels)
        if boundaries:
            # Add table labels
            parent = Boundary.objects.get(pk=tables_boundary)
            response['table_labels'] = {str(poly.pk): poly.data[parent.display_field]
                                        for poly in boundaries}
            # Filter by polygon for counting
            for poly in boundaries:
                counted = (annotated_qs.filter(geom__within=poly.geom)
                                       .values('row', 'col')
                                       .order_by('row', 'col')
                                       .annotate(count=Count('row')))
                response['tables'].append({'tablekey': poly.pk, 'data': counted})
        else:
            counted = (annotated_qs.values('row', 'col')
                                   .order_by('row', 'col')
                                   .annotate(count=Count('row')))
            response['tables'].append({'tablekey': None, 'data': counted})
        return Response(response)

    def _query_param_to_case_stmnt(self, param, request):
        """Wrapper to handle getting the params for each case generator because we do it twice"""
        try:
            record_type = request.query_params['record_type']
        except KeyError:
            raise ParseError(detail="The 'record_type' parameter is required")
        if param.endswith('period_type'):
            return self._make_period_case(request.query_params[param], record_type)
        elif param.endswith('boundary_id'):
            return self._make_boundary_case(request.query_params[param])
        else:  # 'choices_path'; ensured by parent function
            schema = RecordType.objects.get(pk=record_type).get_current_schema()
            return self._make_choices_case(schema, request.query_params[param].split(','))

    def _make_period_case(self, period_type, record_type_id):
        """Constructs a Django Case statement for a certain type of period.

        Args:
            period_type (string): One of 'hour', 'day', 'week_day', 'week', 'month', 'year'
            record_type_id (string): Record type to use (uuid)
        Returns:
            (Case, labels), where Case is a Django Case object giving the period in which each
            record's occurred_from falls, and labels is a dict mapping Case values to period labels

        """
        easy_ranges = {  # Most date-related things are 1-indexed.
            'month': xrange(1, 13),
            'week': xrange(1, 54),  # Up to 53 weeks in a year
            'week_day': xrange(1, 8),
            'day': xrange(1, 32),
            'hour': xrange(0, 24)
        }
        valid_periods = easy_ranges.keys() + ['year']
        if period_type not in valid_periods:
            raise ParseError(detail=('row_/col_period_type must be one of {}; received {}'
                                     .format(valid_periods, period_type)))

        # Set the range min / maxes based on the period type
        period_range = None
        if period_type == 'year':
            recs = Record.objects.filter(schema__record_type_id=record_type_id)
            range_min = recs.order_by('occurred_from').first().occurred_from.year
            range_max = recs.order_by('-occurred_from').first().occurred_from.year + 1
            period_range = xrange(range_min, range_max)
        else:
            period_range = easy_ranges[period_type]

        whens = []  # Eventual list of When-clause objects
        when_lookup = 'occurred_from__{}'.format(period_type)
        for x in period_range:
            when_args = {'then': Value(x)}
            when_args[when_lookup] = x
            whens.append(When(**when_args))
        # TODO: It would be nice to have friendlier names for weekdays and months, but that
        # would introduce potential translation complexity down the road so I'm going to hold off
        # until we get a better understanding of what the upcoming multilingual / date issues look
        # like.
        return (Case(*whens, output_field=IntegerField()), {str(x): str(x) for x in period_range})

    def _make_boundary_case(self, boundary_id):
        """Constructs a Django Case statement for points falling within a particular polygon

        Args:
            boundary_id (uuid): Id of a Boundary whose BoundaryPolygons should be used in the case
        Returns:
            (Case, labels), where Case is a Django Case object outputting the UUID of the polygon
            which contains each record, and labels is a dict mapping boundary pks to their labels.
        """
        boundary = Boundary.objects.get(pk=boundary_id)
        polygons = BoundaryPolygon.objects.filter(boundary=boundary)
        return (Case(*[When(geom__within=poly.geom, then=Value(poly.pk)) for poly in polygons],
                     output_field=UUIDField()),
                {str(poly.pk): poly.data[boundary.display_field] for poly in polygons})

    def _make_choices_case(self, schema, path):
        """Constructs a Django Case statement for the choices of a schema property

        Args:
            schema (RecordSchema): A RecordSchema to get properties from
            path (list): A list of path fragments to navigate to the desired property
        Returns:
            (Case, labels), where Case is a Django Case object with the choice of each record,
            and labels is a dict matching choices to their labels (currently the same).
        """
        # Walk down the schema using the path components
        obj = schema.schema['definitions']  # 'definitions' is the root of all schema paths
        try:
            for key in path:
                obj = obj[key]
        except KeyError as e:
            raise ParseError(detail="Part of choices_path was not found: '{}'".format(e.message))

        # Build a JSONB filter that will catch Records that match each choice in the enum.
        choices = obj.get('enum', None)
        if not choices:
            raise ParseError(detail="The property at choices_path is missing required 'enum' field")
        # Build the djsonb filter specification from the inside out, and skip 'properties' -- it's
        # only for Schemas.
        filter_path = [component for component in reversed(path) if component != 'properties']
        whens = []
        for choice in choices:
            filter_rule = dict(_rule_type='containment', contains=[choice])
            for component in filter_path:
                # Nest filter inside itself so we eventually get something like:
                # {"accidentDetails": {"severity": {"_rule_type": "containment"}}}
                tmp = dict()
                tmp[component] = filter_rule
                filter_rule = tmp
            whens.append(When(data__jsonb=filter_rule, then=Value(choice)))
        return (Case(*whens, output_field=CharField()), {choice: choice for choice in choices})

    def _cache_tile_sql(self, token, sql):
        """Stores a sql string in the common cache so it can be retrieved by Windshaft later"""
        # We need to use a raw Redis connection because the Django cache backend
        # transforms the keys and values before storing them. If the cached data
        # were being read by Django, this transformation would be reversed, but
        # since the stored sql will be parsed by Windshaft / Postgres, we need
        # to store the data exactly as it is.
        redis_conn = get_redis_connection('default')
        redis_conn.set(token, sql.encode('utf-8'))


class DriverRecordAuditLogViewSet(viewsets.ModelViewSet):
    """Viewset for accessing audit logs; will output CSVs if Accept text/csv is specified"""
    queryset = RecordAuditLogEntry.objects.all()
    renderer_classes = api_settings.DEFAULT_RENDERER_CLASSES + [csv_renderer.CSVRenderer]
    serializer_class = RecordAuditLogEntrySerializer
    permission_classes = (IsAdminAndReadOnly,)
    filter_class = filters.RecordAuditLogFilter
    pagination_class = None

    def list(self, request, *args, **kwargs):
        """Validate filter params"""
        # Will throw an error if these are missing or not valid ISO-8601
        try:
            min_date = parse_date(request.query_params['min_date'])
            max_date = parse_date(request.query_params['max_date'])
        except KeyError:
            raise ParseError("min_date and max_date must both be provided")
        except ValueError:
            raise ParseError("occurred_min and occurred_max must both be valid dates")
        # Make sure that min_date and max_date are less than 32 days apart
        if max_date - min_date >= timedelta(days=32):
            raise ParseError('max_date and min_date must be less than one month apart')
        return super(DriverRecordAuditLogViewSet, self).list(request, *args, **kwargs)

    # Override default CSV field ordering and include URL
    def get_renderer_context(self):
        context = super(DriverRecordAuditLogViewSet, self).get_renderer_context()
        context['header'] = self.serializer_class.Meta.fields
        return context


def start_jar_build(schema_uuid):
    """Helper to kick off build of a Dalvik jar file with model classes for a schema.
    Publishes schema with its UUID to a redis channel that the build task listens on.

    :param schema_uuid: Schema UUID, which is the key used to store the jar on redis.
    """
    # Find the schema with the requested UUID.
    schema_model = RecordSchema.objects.get(uuid=schema_uuid)
    if not schema_model:
        return False

    schema = schema_model.schema
    redis_conn = get_redis_connection('jars')
    redis_conn.publish('jar-build', json.dumps({'uuid': schema_uuid, 'schema': schema}))
    return True


# override ashlar views to set permissions and trigger model jar builds
class DriverBoundaryPolygonViewSet(BoundaryPolygonViewSet):
    permission_classes = (IsAdminOrReadOnly,)


class DriverRecordTypeViewSet(RecordTypeViewSet):
    permission_classes = (IsAdminOrReadOnly,)


class DriverRecordSchemaViewSet(RecordSchemaViewSet):
    permission_classes = (IsAdminOrReadOnly,)

    # Filter out everything except details for read-only users
    def get_serializer_class(self):
        if is_admin_or_writer(self.request.user):
            return RecordSchemaSerializer
        return DetailsReadOnlyRecordSchemaSerializer

    def perform_create(self, serializer):
        instance = serializer.save()
        start_jar_build(str(instance.pk))

    def perform_update(self, serializer):
        instance = serializer.save()
        start_jar_build(str(instance.pk))

    def perform_destroy(self, instance):
        redis_conn = get_redis_connection('jars')
        redis_conn.delete(str(instance.pk))
        instance.delete()


class DriverBoundaryViewSet(BoundaryViewSet):
    permission_classes = (IsAdminOrReadOnly,)


class DriverRecordDuplicateViewSet(viewsets.ModelViewSet):
    queryset = RecordDuplicate.objects.all().order_by('record__occurred_to')
    serializer_class = RecordDuplicateSerializer
    permission_classes = (ReadersReadWritersWrite,)
    filter_class = filters.RecordDuplicateFilter

    @detail_route(methods=['patch'])
    def resolve(self, request, pk=None):
        duplicate = self.queryset.get(pk=pk)
        recordUUID = request.data.get('recordUUID', None)
        if recordUUID is None:
            # No record id means they want to keep both, so just resolve the duplicate
            duplicate.resolved = True
            duplicate.save()
            resolved_ids = [duplicate.pk]
        else:
            # If they picked a record, archive the other one and resolve all duplicates involving it
            # (which will include the current one)
            if recordUUID == str(duplicate.record.uuid):
                rejected_record = duplicate.duplicate_record
            elif recordUUID == str(duplicate.duplicate_record.uuid):
                rejected_record = duplicate.record
            else:
                raise Exception("Error: Trying to resolve a duplicate with an unconnected record.")
            rejected_record.archived = True
            rejected_record.save()
            resolved_dup_qs = RecordDuplicate.objects.filter(Q(resolved=False),
                                                             Q(record=rejected_record) |
                                                             Q(duplicate_record=rejected_record))
            resolved_ids = [str(uuid) for uuid in resolved_dup_qs.values_list('pk', flat=True)]
            resolved_dup_qs.update(resolved=True)
        return Response({'resolved': resolved_ids})


class RecordCsvExportViewSet(viewsets.ViewSet):
    """A view for interacting with CSV export jobs

    Since these jobs are not model-backed, we won't use any of the standard DRF mixins
    """
    permissions_classes = (IsAdminOrReadOnly,)

    def retrieve(self, request, pk=None):
        """Return the status of the celery task with query_params['taskid']"""
        # N.B. Celery will never return an error if a task_id doesn't correspond to a
        # real task; it will simply return a task with a status of 'PENDING' that will never
        # complete.
        job_result = export_csv.AsyncResult(pk)
        if job_result.state in states.READY_STATES:
            if job_result.state in states.EXCEPTION_STATES:
                e = job_result.get(propagate=False)
                return Response({'status': job_result.state, 'error': str(e)})
            # Set up the URL to proxy to the celery worker
            # TODO: This won't work with multiple celery workers
            # TODO: We should add a cleanup task to prevent result files from accumulating
            # on the celery worker.
            uri = '{scheme}://{host}{prefix}{file}'.format(scheme=request.scheme,
                                                           host=request.get_host(),
                                                           prefix=settings.CELERY_DOWNLOAD_PREFIX,
                                                           file=str(job_result.get()))
            return Response({'status': job_result.state, 'result': uri})
        return Response({'status': job_result.state, 'info': job_result.info})

    def create(self, request, *args, **kwargs):
        """Create a new CSV export task, using the passed filterkey as a parameter

        filterkey is the same as the "tilekey" that we pass to Windshaft; it must be requested
        from the Records endpoint using tilekey=true
        """
        filter_key = request.data.get('tilekey', None)
        if not filter_key:
            return Response({'errors': {'tilekey': 'This parameter is required'}},
                            status=status.HTTP_400_BAD_REQUEST)

        task = export_csv.delay(filter_key)
        return Response({'success': True, 'taskid': task.id}, status=status.HTTP_201_CREATED)

    # TODO: If we switch to a Django/ORM database backend, we can subclass AbortableTask
    # and allow cancellation as well.


class JarFileRenderer(renderers.BaseRenderer):
    """Renderer for downloading JARs.

    http://www.django-rest-framework.org/api-guide/renderers/#data
    """
    media_type = 'application/java-archive'
    charset = None
    render_style = 'binary'
    format = 'jar'

    def render(self, data, media_type=None, renderer_context=None, format=None):
        return data


class AndroidSchemaModelsViewSet(viewsets.ViewSet):
    """A view for interacting with Android jar build jobs."""

    permissions_classes = (IsAuthenticated,)
    renderer_classes = [JarFileRenderer] + api_settings.DEFAULT_RENDERER_CLASSES

    def finalize_response(self, request, response, *args, **kwargs):
        response = super(AndroidSchemaModelsViewSet, self).finalize_response(request, response, *args, **kwargs)
        if isinstance(response.accepted_renderer, JarFileRenderer):
            response['content-disposition'] = 'attachment; filename=models.jar'
        return response

    def retrieve(self, request, pk=None):
        """Return the model jar if found, or kick of a task to create it if not found.

        If a previous message has already been sent for the same UUID, the new request will be
        ignored by the subscriber, which processes requests serially.
        """

        uuid = str(pk)
        if not uuid:
            return Response({'errors': {'uuid': 'This parameter is required'}},
                            status=status.HTTP_400_BAD_REQUEST)

        redis_conn = get_redis_connection('jars')
        found_jar = redis_conn.get(uuid)
        if not found_jar:
            found = RecordSchema.objects.filter(uuid=uuid).count()
            if found == 1:
                # have a good schema UUID but no jar for it; go kick off a jar build
                start_jar_build(uuid)
                return Response({'success': True}, status=status.HTTP_201_CREATED)
            else:
                return Response({'errors': {'uuid': 'Schema matching UUID not found'}},
                                status=status.HTTP_400_BAD_REQUEST)
        else:
            # update cache expiry, then return the jar binary found in redis
            # to actually download the file, specify format=jar request parameter
            redis_conn.expire(uuid, settings.JARFILE_REDIS_TTL_SECONDS)
            return Response(found_jar)
