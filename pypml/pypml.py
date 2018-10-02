""" Python Parking Monitor Library (PyPML)
    Copyright (C) 2018
    Lara CODECA

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""

import collections
import copy
import logging
import operator
import os
import random
import sys
import xml.etree.ElementTree

# """ Import TraCI library """
if 'SUMO_TOOLS' in os.environ:
    sys.path.append(os.environ['SUMO_TOOLS'])
    import traci
    import traci.constants as tc
else:
    sys.exit("""The PyPML package uses SUMO TraCI API.
             Please declare the environment variable 'SUMO_TOOLS'""")

class ParkingMonitor(traci.StepListener):
    """ SUMO StepListener class for the parking monitoring. """

    _logger = None
    _options = None

    _parking_db = dict()
    _routers_db = dict()
    _vehicles_db = dict()
    _passengers_db = set()

    _edges_routers_mapping = collections.defaultdict(list)

    _blacklisted_edges_pairs = collections.defaultdict(list)
    _static_parking_travel_time = collections.defaultdict(list)

    _traci_handler = None
    _traci_arrived_list = None
    _traci_departed_list = None
    _traci_vehicle_subscription = None
    _traci_simulation_subscriptions = None
    _traci_starting_stop_subscriptions = None
    _traci_ending_stop_subscriptions = None

    ## ===============================      INITIALIZATION      ================================ ##

    def _logs(self):
        """ Log init. """
        self._logger = logging.getLogger('parkingmonitor.ParkingMonitor')
        self._logger.setLevel(self._options['logging']['level'])

        handlers = []
        formatter = logging.Formatter('[%(asctime)s][%(name)s][%(levelname)s] %(message)s')
        if self._options['logging']['filename']:
            # create file handler which logs even debug messages
            file_handler = logging.FileHandler(filename=self._options['logging']['filename'],
                                               mode='w')
            file_handler.setLevel(self._options['logging']['level'])
            file_handler.setFormatter(formatter)
            handlers.append(file_handler)

        if self._options['logging']['stdout']:
            # create console handler with a higher log level
            console_handler = logging.StreamHandler()
            console_handler.setLevel(self._options['logging']['level'])
            console_handler.setFormatter(formatter)
            handlers.append(console_handler)

        if handlers:
            # add the handlers to the logger
            for handler in handlers:
                self._logger.addHandler(handler)
        else:
            self._logger = None

    def __init__(self, traci_handler, options, time=0.0):
        """ Initialize the knowlegde base for the parking monitor.

        traci_handler: already initialized TraCI socket that is going to be used by PyPML
        options:       in order to reduce the number of parameters and increase the flexibility,
                       the complete initialization is done using a dict()
        time:          beginning of the simulation in seconds (for statistical and logging purposes)

        Options format:
        {
            'addStepListener': Boolean. Ff True, pypml is added as step listener in SUMO.
                               In case it's False the function step() must be called by hand every
                               simulation step.
            'logging': {
                'stdout': Boolean. If True, the logging will be both file and stdout.
                'filename': String. Name of the logging file.
                'level': Logging level as defined in the logging library. E.g.: logging.DEBUG
            },
            'sumo_parking_file': String. Path and file name of the SUMO additional file defining the
                                 parkings.
            'blacklist': List of strings. List of the parking IDS ans defined in sumo that must be
                         excluded from the monitoring.
            'vclasses': Set of strings. Set of vTypes, as defined in SUMO, comprehensive of all the
                        vehicles used in the simulation.
            'generic_conf': List. List of generic configuration applied to all the parking areas.
            'specific_conf': Dictionary. Dictionary of configuration applied to specific parking
                             areas.
            'subscriptions': {
                'only_parkings': Boolean. If True, PyPML subscribes only to the vehicles that have
                                 a <stop> define at the beginning of the simulation.
            },
        }

        Example of generic_conf:
        'cond' is an expression (prefix notation) that is going to be evaluate agains all the
               parking areas.
        'set_to' is a list of 'parameter' -> {value/expression} that are going to be applied if
                 'cond' is true.
        [
            {
                'cond': ['>', 'total_capacity', 50],
                'set_to': [
                    ['uncertainty', {
                        'mu': 0.0,
                        'sigma': ['*', 'total_capacity', 0.20]
                        }
                    ],
                ],
            },
        ]
        In this example, every parkign area with more than 50 places will have an uncertainty with
            sigma equal to 20% of the capacity.

        Example of specific_conf:
        Each parking area needs a specific entry in the dictionary.
        The values that can be set are:
            'capacity_by_class'
            'subscriptions_by_class'
            'uncertainty'
        and the values are assigned, so they must be complete.

        {
            'parking_id': {
                'capacity_by_class': {
                    'vType': number,
                    ...
                    'vType': number,
                },
            },
        }
        """

        self._options = options

        ## Logs initialization
        self._logs()

        ## TraCI initialization
        self._traci_handler = traci_handler

        ## Read parkings and routers from SUMO add.xml
        self._load_parkings_and_routers()

        ## Populate the parking configurations
        total = 0
        for pid, parking in self._parking_db.items():
            capacity = int(
                self._traci_handler.simulation.getParameter(pid, 'parkingArea.capacity'))
            occupancy = int(
                self._traci_handler.simulation.getParameter(pid, 'parkingArea.occupancy'))
            parking['total_capacity'] = capacity   # TraCI value
            parking['total_occupancy'] = occupancy # TraCI value
            parking['occupancy_series'] = [(occupancy, time)]
            parking['occupancy_by_class'] = dict()
            parking['projections_by_class'] = dict()
            for vclass in self._options['vclasses']:
                parking['occupancy_by_class'][vclass] = set()
                parking['projections_by_class'][vclass] = set()

            ## Set DEFAULT values.
            parking['capacity_by_class'] = dict()
            parking['subscriptions_by_class'] = dict()
            parking['uncertainty'] = {
                'mu': 0.0,
                'sigma': 0.0,
            }

            ## Apply GENERAL CONFIGURATIONS
            for gopt in options['generic_conf']:
                if self._parse_generic_condition(gopt['cond'], parking):
                    for key, value in gopt['set_to']:
                        if key == 'uncertainty':
                            parking['uncertainty'] = {
                                'mu': self._eval_expression(value['mu'], parking),
                                'sigma': self._eval_expression(value['sigma'], parking),
                            }
                        else:
                            ## not sure what this can be
                            parking[key] = copy.deepcopy(value)

            ## Apply SPECIFIC VALUES
            if pid in options['specific_conf'].keys():
                if 'capacity_by_class' in options['specific_conf'][pid]:
                    parking['capacity_by_class'] = copy.deepcopy(
                        options['specific_conf'][pid]['capacity_by_class'])
                if 'subscriptions_by_class' in options['specific_conf'][pid]:
                    parking['subscriptions_by_class'] = copy.deepcopy(
                        options['specific_conf'][pid]['subscriptions_by_class'])
                if 'uncertainty' in options['specific_conf'][pid]:
                    parking['uncertainty'] = {
                        'mu': self._eval_expression(
                            options['specific_conf'][pid]['uncertainty']['mu'], parking),
                        'sigma': self._eval_expression(
                            options['specific_conf'][pid]['uncertainty']['sigma'], parking),
                    }

            total += capacity

        if self._logger:
            self._logger.info('Monitoring %s parkings with a total capacity of %d.',
                              len(self._parking_db), total)

        ## Parkings subscriptions
        self._traci_handler.simulation.subscribe(varIDs=(tc.VAR_PARKING_STARTING_VEHICLES_IDS,
                                                         tc.VAR_PARKING_ENDING_VEHICLES_IDS))

        ## StepListener registration
        if self._options['addStepListener']:
            self._traci_handler.addStepListener(self)

    def _load_parkings_and_routers(self):
        """ Load the parking and routers definition from SUMO add.xml and apply restrictions. """

        filename = self._options['sumo_parking_file']
        xml_tree = xml.etree.ElementTree.parse(filename).getroot()

        for child in xml_tree:
            if child.tag == 'parkingArea' and child.attrib['id'] not in self._options['blacklist']:
                self._parking_db[child.attrib['id']] = {
                    'sumo': child.attrib,
                }
            ## rerouters
            elif child.tag == 'rerouter':
                self._routers_db[child.attrib['id']] = {
                    'id': child.attrib['id'],
                    'edges': child.attrib['edges'].strip(' ').split(' '),
                    'intervals': list(),
                }

                ## given and edge, retrieve the list of routers associated.
                for edge in self._routers_db[child.attrib['id']]['edges']:
                    self._edges_routers_mapping[edge].append(child.attrib['id'])

                for interval in child:
                    _end = int(interval.attrib['end']) * 1000 # interval in milliseconds
                    parkings = list()
                    for area in interval:
                        if 'visible' in area.attrib and area.attrib['visible'] == 'true':
                            parkings.append((area.attrib['id'], True))
                        else:
                            parkings.append((area.attrib['id'], False))
                    self._routers_db[child.attrib['id']]['intervals'].append((_end, parkings))

        if self._routers_db:
            if self._logger:
                self._logger.warning("""The parking rerouters that are define in SUMO additional
                                     files have priority over any TraCI API
                                     (e.g. traci.vehicle.rerouteParkingArea).""")


    @staticmethod
    def _get_operator(oper):
        """ Return the function associated to the operator. """
        return {
            'and': operator.and_,
            'or': operator.or_,
            '>': operator.gt,
            '>=': operator.ge,
            '=': operator.eq,
            '<=': operator.le,
            '<': operator.lt,
            '+': operator.add,
            '-': operator.sub,
            '*': operator.mul,
            '/': operator.truediv,
            '**': operator.pow,
        }[oper]

    def _parse_generic_condition(self, condition, environment):
        """ Parse the generic condition and returns True or False accordingly. """
        oper, operand_1, operand_2 = condition

        if isinstance(operand_1, list):
            operand_1 = self._parse_generic_condition(operand_1, environment)
        if isinstance(operand_2, list):
            operand_2 = self._parse_generic_condition(operand_2, environment)

        if operand_1 in environment:
            operand_1 = environment[operand_1]
        if operand_2 in environment:
            operand_2 = environment[operand_2]

        return self._get_operator(oper)(operand_1, operand_2)

    def _eval_expression(self, expr, environment):
        """ Evaluate an expression in the enviroment. """
        ## usual case with simple evaluation
        if not isinstance(expr, list):
            if expr in environment:
                return environment[expr]
            return expr

        ## complex expression
        oper, operand_1, operand_2 = expr

        if isinstance(operand_1, list):
            operand_1 = self._eval_expression(operand_1, environment)
        if isinstance(operand_2, list):
            operand_2 = self._eval_expression(operand_2, environment)

        if operand_1 in environment:
            operand_1 = environment[operand_1]
        if operand_2 in environment:
            operand_2 = environment[operand_2]

        return self._get_operator(oper)(operand_1, operand_2)

    ## ===============================         OVERLOADS         =============================== ##

    def step(self, t=0):
        """ TraCI StepListener caller.

            In order to be independent from the call, t is not used and the time is directly
            retrieved using simulation.getTime().
        """
        time = self._traci_handler.simulation.getTime()
        self._monitor_vehicles(time)
        self._update_vehicles_db(time)
        self._update_parking_db(time)
        return True

    ## ===============================         UTILITIES         =============================== ##

    @staticmethod
    def is_parking_area(flags):
        """ isStoppedParking(string) -> bool
            Return whether the vehicle is parking (implies stopped)

            The flags integer is defined as
               1 * stopped +
               2 * parking +
               4 * personTriggered +
               8 * containerTriggered +
              16 * isBusStop +
              32 * isContainerStop +
              64 * chargingStation +
             128 * parkingarea
            with each of these flags defined as 0 or 1
        """
        return (flags & 128) == 128

    def get_parking_access(self, parking):
        """ Given a parking ID, returns the lane information.
            Raise an Exception in canse the requested parking id does not exist.

            parking: String. Parking area ID as defined in SUMO.
        """
        if parking in self._parking_db.keys():
            return self._parking_db[parking]['sumo']['lane']
        raise Exception('Parking {} does not exist.'.format(parking))

    ## ===============================         MONITORING        =============================== ##

    def _monitor_vehicles(self, step):
        """ Create subscriptions for the vehicles with planned stops in parking areas. """
        self._traci_departed_list = self._traci_handler.simulation.getDepartedIDList()
        for vehicle in self._traci_departed_list:
            v_class = self._traci_handler.vehicle.getVehicleClass(vehicle)
            if self._options['subscriptions']['only_parkings'] and v_class in ['bus', 'rail']:
                continue

            stops = self._traci_handler.vehicle.getNextStops(vehicle)
            current_stops = list()
            _new_stops = set()
            for stop in stops:
                _, _, stopping_place, stop_flags, _, _ = stop
                if self.is_parking_area(stop_flags):
                    current_stops.append(stop)
                    _new_stops.add(stopping_place)

            if self._options['subscriptions']['only_parkings'] and not current_stops:
                continue

            passengers = self._traci_handler.vehicle.getPersonIDList(vehicle)
            for passenger in passengers:
                self._passengers_db.add(passenger)

            if self._logger:
                self._logger.debug('[%d] Vehicle %s added to subscriptions.', step, vehicle)
            self._traci_handler.vehicle.subscribe(
                vehicle, varIDs=(tc.VAR_ROAD_ID, tc.VAR_NEXT_STOPS, tc.LAST_STEP_PERSON_ID_LIST))

            self._vehicles_db[vehicle] = {
                'id': vehicle,
                'departure': step,
                'edge': '',
                'stops': current_stops,
                'history': [],
                'vClass': v_class,
                'passengers': passengers,
                'arrived': None,
            }

            ## update parking projections
            for area in _new_stops:
                self._parking_db[area]['projections_by_class'][v_class].add(vehicle)

        self._traci_arrived_list = self._traci_handler.simulation.getArrivedIDList()
        for vehicle in self._traci_arrived_list:
            if vehicle in self._vehicles_db:
                self._vehicles_db[vehicle]['arrived'] = step

    @staticmethod
    def _is_same_destinations(a_stops, b_stops):
        """ Return true iff the series of stops ids is the same. """
        if len(a_stops) != len(b_stops):
            return False
        nmr_stops = len(a_stops)
        for pos in range(nmr_stops):
            if a_stops[pos][2] != b_stops[pos][2]:
                return False
        return True

    def _update_vehicles_db(self, step):
        """ Update subscriptions and vechiles database. """
        self._traci_vehicle_subscription = self._traci_handler.vehicle.getAllSubscriptionResults()
        for vehicle, data in self._traci_vehicle_subscription.items():
            ## always to update
            self._vehicles_db[vehicle]['edge'] = data[tc.VAR_ROAD_ID]
            self._vehicles_db[vehicle]['passengers'] = data[tc.LAST_STEP_PERSON_ID_LIST]
            for passenger in data[tc.LAST_STEP_PERSON_ID_LIST]:
                self._passengers_db.add(passenger)

            ## stop check
            stops = data[tc.VAR_NEXT_STOPS]
            current_stops = list()
            _new_stops = set()
            for stop in stops:
                _, _, stopping_place, stop_flags, _, _ = stop
                if self.is_parking_area(stop_flags):
                    current_stops.append(stop)
                    _new_stops.add(stopping_place)

            if self._is_same_destinations(self._vehicles_db[vehicle]['stops'], current_stops):
                ## nothing changed
                continue

            if self._logger:
                self._logger.debug('[%d] Stop change for %s.', step, vehicle)

            ## update parking projections
            _old_stops = set()
            for _, _, _stop, _, _, _ in self._vehicles_db[vehicle]['stops']:
                _old_stops.add(_stop)
            v_class = self._vehicles_db[vehicle]['vClass']
            for area in _old_stops - _new_stops:
                self._parking_db[area]['projections_by_class'][v_class].remove(vehicle)
            for area in _new_stops - _old_stops:
                self._parking_db[area]['projections_by_class'][v_class].add(vehicle)

            ## update stops
            self._vehicles_db[vehicle]['history'].append(
                self._vehicles_db[vehicle]['stops'])
            self._vehicles_db[vehicle]['stops'] = current_stops

            ## to set it only once whan the current stops are empty the first time.
            if 'final_stop_arrival' not in self._vehicles_db[vehicle] and not current_stops:
                self._vehicles_db[vehicle]['final_stop_arrival'] = step

            if self._options['subscriptions']['only_parkings'] and not current_stops:
                if self._logger:
                    self._logger.debug('[%d] Unsubscribing from vehicle %s, no additional stops.',
                                       step, vehicle)
                try:
                    self._traci_handler.vehicle.unsubscribe(vehicle)
                except traci.exceptions.TraCIException:
                    if self._logger:
                        self._logger.critical('[%d] Unsubscription failed.', step)

    def _check_occupancy(self, step):
        """ Gather parking current occupancy. """
        for parking in self._parking_db:
            occupancy = int(self._traci_handler.simulation.getParameter(parking,
                                                                        'parkingArea.occupancy'))
            if self._parking_db[parking]['total_occupancy'] != occupancy:
                self._parking_db[parking]['occupancy_series'].append((occupancy, step))
                self._parking_db[parking]['total_occupancy'] = occupancy

    def _get_parking_area_from_vehicle(self, vehicle):
        """ Return the parking area ID of the 'current' stop. """
        if self._vehicles_db[vehicle]['history']:
            return self._vehicles_db[vehicle]['history'][-1][0][2]

    def _update_parking_db(self, step):
        """ Update subscriptions and parking database. """

        self._check_occupancy(step)

        _to_validate = set()

        ## VAR_PARKING_ENDING_VEHICLES_IDS are delayed 1 TS.
        if self._traci_ending_stop_subscriptions:
             ## Update parking capacity by vClass
            for vehicle in self._traci_ending_stop_subscriptions:
                parking_area = self._get_parking_area_from_vehicle(vehicle)
                if parking_area in self._parking_db:
                    v_class = self._vehicles_db[vehicle]['vClass']
                    try:
                        self._parking_db[parking_area]['occupancy_by_class'][v_class].remove(
                            vehicle)
                    except KeyError:
                        if self._logger:
                            self._logger.critical('[%d] Vehicle %s cannot be removed from area %s',
                                                  step, vehicle, parking_area)
                        raise Exception('[{}] Vehicle {} cannot be removed from area {}.'.format(
                            step, vehicle, parking_area))
                    _to_validate.add(parking_area)
                else:
                    if self._logger:
                        self._logger.debug('[%d] Parking area %s not monitored.',
                                           step, parking_area)

        self._traci_simulation_subscriptions = (
            self._traci_handler.simulation.getAllSubscriptionResults())
        self._traci_starting_stop_subscriptions = (
            self._traci_simulation_subscriptions[''][tc.VAR_PARKING_STARTING_VEHICLES_IDS])
        self._traci_ending_stop_subscriptions = (
            self._traci_simulation_subscriptions[''][tc.VAR_PARKING_ENDING_VEHICLES_IDS])

        if self._traci_starting_stop_subscriptions:
            ## Update parking capacity by vClass
            for vehicle in self._traci_starting_stop_subscriptions:
                if vehicle not in self._vehicles_db:
                    if self._logger:
                        self._logger.critical('[%d] Vehicle %s is parked but not in the DB.',
                                              step, vehicle)
                parking_area = self._get_parking_area_from_vehicle(vehicle)
                if parking_area in self._parking_db:
                    v_class = self._vehicles_db[vehicle]['vClass']
                    self._parking_db[parking_area]['occupancy_by_class'][v_class].add(vehicle)
                    _to_validate.add(parking_area)
                else:
                    if self._logger:
                        self._logger.debug('[%d] Parking area %s not monitored.',
                                           step, parking_area)

        for pid in _to_validate:
            self._validate_parking_occupancy(pid)

    ## ===============================    TRACI SUBSCRIPTIONS     ============================== ##

    def get_traci_vehicle_subscriptions(self):
        """ Return TraCI vehicle subscriptions for the last simulation-step. """
        return copy.deepcopy(self._traci_vehicle_subscription)

    def get_traci_simulation_subscriptions(self):
        """ Return TraCI simulation subscriptions for the last simulation-step. """
        return copy.deepcopy(self._traci_simulation_subscriptions)

    ## ===============================          REROUTERS         ============================== ##

    def get_rerouter_iterator(self, step):
        """ Return the rerouter info for the given step.

            step: Float. Simulation time in seconds.
        """
        for value in self._routers_db.values():
            current = None
            for end, parkings in value['intervals']:
                if not current:
                    current = parkings
                if step <= end:
                    current = parkings
                else:
                    break
            yield {
                'id': value['id'],
                'edges': value['edges'],
                'info': current,
            }

    ## ===============================         PASSENGERS        =============================== ##

    def get_passenger_iterator(self):
        """ Return the passenger iterator. """
        for value in self._passengers_db:
            yield value

    ## ===============================          VEHICLES         =============================== ##

    def get_vehicle_iterator(self):
        """ Return the vehicle info. """
        for value in self._vehicles_db.values():
            yield value

    def get_vehicle(self, vehicle):
        """ Return the vehicle with the given ID or None if not existent.

            vehicle: String. Vehicle ID as defined in SUMO.
        """
        if vehicle in self._vehicles_db:
            return copy.deepcopy(self._vehicles_db[vehicle])
        return None

    def set_vehicle_param(self, vehicle, param, value):
        """ Set the param=value in the vehicle with the given ID.
            Returns False if the vehicle does not exist.

            vehicle: String. Vehicle ID as defined in SUMO.
            param:   String. Parameters name.
            value:   Any. Value for the parameter.
            """
        if vehicle in self._vehicles_db:
            self._vehicles_db[vehicle][param] = copy.deepcopy(value)
            return True
        return False

    ## ===============================          PARKINGS         =============================== ##

    def get_parking_iterator(self):
        """ Return the parking iterator. """
        for value in self._parking_db.values():
            yield copy.deepcopy(value)

    def get_parking(self, parking):
        """ Return the parking area with the given ID or None if not existent.

            parking: String. Parking area ID as defined in SUMO.
        """
        if parking in self._parking_db:
            return copy.deepcopy(self._parking_db[parking])
        return None

    def compute_parking_travel_time(self):
        """ For each parking, saves the parkings reachable by 'passenger' vClass where the weight
            is the travel time at the current stage of the simulation.
            Each call of the funcion destroy the previous state.
        """

        self._static_parking_travel_time = collections.defaultdict(list)

        parkings = []
        for parking in self._parking_db.values():
            pid = parking['sumo']['id']
            edge = parking['sumo']['lane'].split('_')[0]
            end_pos = float(parking['sumo']['endPos'])
            parkings.append((pid, edge, end_pos))

        for from_pid, from_edge, from_end_pos in parkings:
            for to_pid, to_edge, to_end_pos in parkings:
                if from_pid == to_pid:
                    continue
                if from_edge == to_edge and to_end_pos <= from_end_pos:
                    ## parking not reachable
                    continue
                if to_edge in self._blacklisted_edges_pairs[from_edge]:
                    ##  route not available
                    continue

                route = None
                try:
                    route = self._traci_handler.simulation.findRoute(from_edge, to_edge,
                                                                     vType='passenger')
                except traci.exceptions.TraCIException:
                    route = None
                    self._blacklisted_edges_pairs[from_edge].append(to_edge)

                cost = None
                if route and route.edges:
                    cost = route.travelTime

                if cost:
                    self._static_parking_travel_time[from_pid].append((cost, to_pid))

        for distances in self._static_parking_travel_time.values():
            distances.sort()

    def get_closest_parkings(self, parking, num=None):
        """ Return the 'num' closest parkings by travel time from the requested parking.
            It requires the travel time structure initialization using
            compute_parking_travel_time(), it raises an Exception if the structure is not yet
            initialized.

            parking: String. Parking area ID as defined in SUMO.
            num:     Int. Number of element to be returned.
        """

        if not self._static_parking_travel_time:
            raise Exception('Estimated travel time structure for parkings is not initialized.')

        parkings = []
        if parking in self._static_parking_travel_time:
            for item in self._static_parking_travel_time[parking]:
                if num and len(parkings) == num:
                    break
                parkings.append(item)
        return parkings

    ## ============================     PARKING SUBSCRIPTIONS      ============================= ##

    def get_parking_subscriptions(self, parking):
        """ Given a parking ID, returns the subscriptions information.
            Raises an Exception if the requested parking area does not exist.

            parking: String. Parking area ID as defined in SUMO.
        """
        if parking in self._parking_db.keys():
            return copy.deepcopy(self._parking_db[parking]['subscriptions_by_class'])
        raise Exception('Parking {} does not exist.'.format(parking))

    def set_parking_subscriptions(self, parking, subscriptions):
        """ Set the given subsctiption to the parking id.
            Raises an Exception if the requested parking area does not exist or
            if the subscriptions are not in a valid format.

            parking:       String. Parking area ID as defined in SUMO.
            subscriptions: Dict. { 'vType': int, .., 'vType': int }
        """
        if parking in self._parking_db:
            self._parking_db[parking]['subscriptions_by_class'] = copy.deepcopy(subscriptions)
            self._validate_parking_subscriptions(parking)
        else:
            raise Exception('Parking {} does not exist.'.format(parking))

    def subscribe_vehicle_to_parking(self, parking, vclass, vehicle):
        """ Add the vehicle to the subscription list of the parking area.
            Returns False iif the number of already subscribed vehicles is equal to the number
            of spots available for that specific vclass.
            Raises an Exception if the requested parking area does not exist or
            if the subscriptions are not initialized.

            parking: String. Parking area ID as defined in SUMO.
            vclass:  String. vType as defined in SUMO.
            vehicle: String. Vehicle ID as defined in SUMO.
        """
        if parking in self._parking_db:
            if vclass in self._parking_db[parking]['subscriptions_by_class']:
                _capacity, vehicles = self._parking_db[parking]['subscriptions_by_class'][vclass]
                if vehicle in vehicles:
                    return False
                if len(vehicles) < _capacity:
                    vehicles.add(vehicle)
                    return True
                # subscription full
                return False
            else:
                raise Exception('vClass "{}" not initialized in parking {}.'.format(
                    vclass, parking))
        else:
            raise Exception('Parking {} does not exist.'.format(parking))

    def remove_subscribed_vehicle(self, parking, vclass, vehicle):
        """ Remove the vehicles from the subscriptions of the given parking id.
            Raises an Exception if the requested parking area does not exist or
            if the subscriptions are not initialized.

            parking: String. Parking area ID as defined in SUMO.
            vclass:  String. vType as defined in SUMO.
            vehicle: String. Vehicle ID as defined in SUMO.
        """
        if parking in self._parking_db:
            if vclass in self._parking_db[parking]['subscriptions_by_class']:
                _capacity, vehicles = self._parking_db[parking]['subscriptions_by_class'][vclass]
                if vehicle in vehicles:
                    vehicles.remove(vehicle)
                    return True
                # vehicle not found
                return False
            else:
                raise Exception('vClass "{}" not initialized in parking {}.'.format(
                    vclass, parking))
        else:
            raise Exception('Parking {} does not exist.'.format(parking))

    ## ============================       PARKING PROJECTIONS      ============================= ##

    def get_parking_projections(self, parking):
        """ Given a parking ID, returns the projections information.
            Raises an Exception if the requested parking area does not exist.

            parking: String. Parking area ID as defined in SUMO.
        """
        if parking in self._parking_db.keys():
            return copy.deepcopy(self._parking_db[parking]['projection_by_class'])
        raise Exception('Parking {} does not exist.'.format(parking))

    ## ============================  PARKING CAPACITY - OCCUPANCY  ============================= ##

    def get_free_places(self, parking, with_uncertainty=False,
                        vclass=None, with_projections=False, with_subscriptions=False):
        """ Returns the free places in a given parking area.
            Raises an Exception if the requested parking area does not exist.

            parking:            String. Parking area ID as defined in SUMO.
            with_uncertainty:   Boolean. If True, uncertainty is applied.
            vclass:             String. If set, returns values only for the specified vType.
            with_projections:   Boolean. If True, projections are taken into account.
            with_subscriptions: Boolean. If True, subscriptions are taken into account.
        """

        if parking not in self._parking_db.keys():
            raise Exception('Parking {} does not exist.'.format(parking))

        error = 0
        if with_uncertainty:
            error = round(random.normalvariate(self._parking_db[parking]['uncertainty']['mu'],
                                               self._parking_db[parking]['uncertainty']['sigma']))

        current_capacity = dict()
        for key, capacity in self._parking_db[parking]['capacity_by_class'].items():
            current_capacity[key] = capacity

        total_occupancy = self._parking_db[parking]['total_occupancy']
        occupancy = dict()
        for key, values in self._parking_db[parking]['occupancy_by_class'].items():
            occupancy[key] = set(values)

        total_projections = 0
        if with_projections:
            for key, vehicles in self._parking_db[parking]['projections_by_class'].items():
                total_projections += len(vehicles)
                occupancy[key] = occupancy[key] | vehicles

        total_subscriptions = 0
        partial_subscriptions = 0
        subscriptions = dict()
        if with_subscriptions:
            for key, (num, veh) in self._parking_db[parking]['subscriptions_by_class'].items():
                subscriptions[key] = num - len(veh)
                occupancy[key] = occupancy[key] | veh
                total_subscriptions += num
                partial_subscriptions += subscriptions[key]

        if current_capacity:
            for key, vehicles in occupancy.items():
                current_capacity[key] += error
                current_capacity[key] -= len(vehicles)
                if with_subscriptions and subscriptions:
                    current_capacity[key] -= subscriptions[key]
            if vclass in current_capacity:
                return current_capacity[vclass]
            return current_capacity

        return (self._parking_db[parking]['total_capacity'] - total_occupancy -
                total_projections - total_subscriptions + error)

    def get_parking_capacity_vclass(self, parking):
        """ Given a parking ID, returns the capacity by vclass information.
            Raises an Exception if the requested parking area does not exist.

            parking: String. Parking area ID as defined in SUMO.
        """
        if parking in self._parking_db.keys():
            return copy.deepcopy(self._parking_db[parking]['capacity_by_class'])
        raise Exception('Parking {} does not exist.'.format(parking))

    def set_parking_capacity_vclass(self, parking, capacities):
        """ Set the given capacity by vclass to the parking id.
            Raises an Exception if the requested parking area does not exist or
            if the capacities are not in a valid format.

            parking:    String. Parking area ID as defined in SUMO.
            capacities: Dict. { 'vType': int, .., 'vType': int }
        """
        if parking in self._parking_db:
            self._parking_db[parking]['capacity_by_class'] = copy.deepcopy(capacities)
            self._validate_parking_capacity(parking)
        else:
            raise Exception('Parking {} does not exist.'.format(parking))

    ## ============================       PARKING VALIDATION       ============================= ##

    def _validate_parking_capacity(self, parking):
        """ Checks if the sum of all the 'capacity_by_class' matches the 'total_capacity. """

        if set(self._parking_db[parking]['capacity_by_class'].keys()) != self._options['vclasses']:
            raise Exception("""The vClasses in "capacity_by_class" of {} must be all and """
                            """only {} [see parameter "vclasses"].""".format(
                                parking, self._options['vclasses']))

        total = 0
        for value in self._parking_db[parking]['capacity_by_class'].values():
            total += value
        if total != self._parking_db[parking]['total_capacity']:
            raise Exception("""The total capacity for parking area {} is {} but it must be """
                            """equal to the one defined in SUMO: {}.""".format(
                                parking, total, self._parking_db[parking]['total_capacity']))

    def _validate_parking_occupancy(self, parking):
        """ Checks if the sum of all the 'occupancy_by_class' matches the 'total_occupancy. """

        if set(self._parking_db[parking]['occupancy_by_class'].keys()) != self._options['vclasses']:
            raise Exception("""The vClasses in "occupancy_by_class" of {} must be all and """
                            """only {} [see parameter "vclasses"].""".format(
                                parking, self._options['vclasses']))

        total = 0
        for v_class, value in self._parking_db[parking]['occupancy_by_class'].items():
            if self._parking_db[parking]['capacity_by_class']:
                info = (
                    "The occupancy in parking area {} for vType {} is {} of {}.".format(
                        parking, v_class, len(value),
                        self._parking_db[parking]['capacity_by_class'][v_class]))
                if self._logger:
                    self._logger.debug(info)
                if len(value) > self._parking_db[parking]['capacity_by_class'][v_class]:
                    raise Exception(info)
            total += len(value)
        if total != self._parking_db[parking]['total_occupancy']:
            raise Exception("""The total occupancy for parking area {} is {} but it must be """
                            """equal to the one retrieved from SUMO: {}.""".format(
                                parking, total, self._parking_db[parking]['total_occupancy']))

    def _validate_parking_subscriptions(self, parking):
        """ Checks if the sum of all the 'subscriptions_by_class' matches the 'total_occupancy. """

        if not self._parking_db[parking]['capacity_by_class']:
            raise Exception("""Parking subscriptions for parking {} cannot be set without """
                            """setting 'capacity_by_class' in advance.""".format(parking))

        if (set(self._parking_db[parking]['subscriptions_by_class'].keys())
                != self._options['vclasses']):
            raise Exception("""The vClasses in "subscriptions_by_class" of {} must be all and """
                            """only {} [see parameter "vclasses"].""".format(
                                parking, self._options['vclasses']))

        for key, value in self._parking_db[parking]['subscriptions_by_class'].items():
            if value[0] > self._parking_db[parking]['capacity_by_class'][key]:
                raise Exception(
                    "In parking {}, subscription for {} exceed the capacity [{}/{}].".format(
                        parking, key, value, self._parking_db[parking]['capacity_by_class'][key]))

    ## ========================================================================================= ##
