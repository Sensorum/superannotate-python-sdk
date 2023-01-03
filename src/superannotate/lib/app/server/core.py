import json
import typing

from werkzeug.exceptions import HTTPException
from werkzeug.routing import Map
from werkzeug.routing import Rule
from werkzeug.serving import run_simple
from werkzeug.wrappers import Request
from werkzeug.wrappers import Response


class SingletonMeta(type):
    _instances = {}

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            instance = super().__call__(*args, **kwargs)
            cls._instances[cls] = instance
        return cls._instances[cls]


class SAServer(metaclass=SingletonMeta):
    def __init__(self):
        self._url_map: Map = Map([])
        self._view_function_map: typing.Dict[str, typing.Callable] = {}

    def route(self, rule: str, methods: typing.List[str] = None, **options: typing.Any):
        """Decorate a view function to register it with the given URL
        rule and options. Calls :meth:`add_url_rule`, which has more
        details about the implementation.

        .. code-block:: python

            @route("/")
            def index():
                return "Hello, World!"

        The endpoint name for the route defaults to the name of the view
        function if the ``endpoint`` parameter isn't passed.

        The ``methods`` parameter defaults to ``["GET"]``. ``HEAD`` and
        ``OPTIONS`` are added automatically.

        :param rule: The URL rule string.
        :type rule: str.

        :param methods: Allowed HTTP methods.
        :type rule: list of str

        :param options: Extra options passed to the
            :class:`~werkzeug.routing.Rule` object.
        :type options: list of any


        """

        def decorator(f):
            endpoint = options.pop("endpoint", None)
            options["methods"] = methods
            if not endpoint:
                endpoint = f.__name__
            self.add_url_rule(rule, endpoint, f, **options)

        return decorator

    def add_url_rule(
        self,
        rule: str,
        endpoint: str = None,
        view_func: typing.Callable = None,
        **options: typing.Any
    ):
        """
        Register a rule for routing incoming requests and building
        URLs. The :meth:`route` decorator is a shortcut to call this
        with the ``view_func`` argument. These are equivalent:

        .. code-block:: python

            @app.route("/")
            def index():
                ...

        .. code-block:: python

            def index():
                ...

            app.add_url_rule("/", view_func=index)

        See :ref:`url-route-registrations`.

        The endpoint name for the route defaults to the name of the view
        function if the ``endpoint`` parameter isn't passed. An error
        will be raised if a function has already been registered for the
        endpoint.

        The ``methods`` parameter defaults to ``["GET"]``. ``HEAD`` is
        always added automatically, and ``OPTIONS`` is added
        automatically by default.

        ``view_func`` does not necessarily need to be passed, but if the
        rule should participate in routing an endpoint name must be
        associated with a view function at some point with the
        :meth:`endpoint` decorator.

        .. code-block:: python

            app.add_url_rule("/", endpoint="index")

            @app.endpoint("index")
            def index():
                ...

        If ``view_func`` has a ``required_methods`` attribute, those
        methods are added to the passed and automatic methods. If it
        has a ``provide_automatic_methods`` attribute, it is used as the
        default if the parameter is not passed.
        :param rule: The URL rule string.
        :type rule: str

        :param endpoint: Endpoint name.
        :type endpoint: str

        :param view_func: Handler function.
        :type view_func: typing.Callable

        :param options: Extra options passed to the
            :class:`~werkzeug.routing.Rule` object.
        :type options: list of any
        """
        self._url_map.add(Rule(rule, endpoint=endpoint, **options))
        self._view_function_map[endpoint] = view_func

    def _dispatch_request(self, request):
        """Dispatches the request."""
        adapter = self._url_map.bind_to_environ(request.environ)
        try:
            endpoint, values = adapter.match()
            view_func = self._view_function_map.get(endpoint)
            if not view_func:
                return Response(status=404)
            response = view_func(request, **values)
            if isinstance(response, dict):
                return Response(json.dumps(response), content_type="application/json")
            return Response(response)
        except HTTPException as e:
            return e

    def wsgi_app(self, environ, start_response):
        """WSGI application that processes requests and returns responses."""
        request = Request(environ)
        response = self._dispatch_request(request)
        return response(environ, start_response)

    def __call__(self, environ, start_response):
        """The WSGI server calls this method as the WSGI application."""
        return self.wsgi_app(environ, start_response)

    def run(
        self,
        host: str = "localhost",
        port: int = 8000,
        use_debugger=True,
        use_reloader=True,
        ssl_context=None,
        **kwargs
    ):
        """Start a development server for a WSGI application.

        .. warning::

            Do not use the development server when deploying to production.
            It is intended for use only during local development. It is not
            designed to be particularly efficient, stable, or secure.

        :param host: The host to bind to, for example ``'localhost'``.
            Can be a domain, IPv4 or IPv6 address, or file path starting
            with ``unix://`` for a Unix socket.
        :param port: The port to bind to, for example ``8080``. Using ``0``
            tells the OS to pick a random free port.
        :param use_reloader: Use a reloader process to restart the server
            process when files are changed.
        :param use_debugger: Use Werkzeug's debugger, which will show
            formatted tracebacks on unhandled exceptions.
        :param ssl_context: Configure TLS to serve over HTTPS. Can be an
            :class:`ssl.SSLContext` object, a ``(cert_file, key_file)``
            tuple to create a typical context, or the string ``'adhoc'`` to
            generate a temporary self-signed certificate.
        """
        run_simple(
            host,
            port,
            self,
            use_debugger=use_debugger,
            use_reloader=use_reloader,
            ssl_context=ssl_context,
            **kwargs
        )
