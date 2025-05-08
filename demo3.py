import decimal
from itertools import chain
from typing import List, Tuple
from django.conf import settings
from decimal import Decimal
from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _
from rest_framework.exceptions import APIException
from django.db.models import Q
from django.utils import timezone
from my_cloudfly.billing.api.loyalty_program.serializers import RewardProgramClientSerializer
from my_cloudfly.billing.models import Order, LoyaltyProgram, OrderItem, ConfigurableOptionCycle
from my_cloudfly.billing.models.order_reward_and_loyalty import OrderPointReward
from my_cloudfly.billing.models.reward_and_loyalty import ProgramCondition, RewardProgram, ProgramTypes
from my_cloudfly.billing.models.types import OrderItemTypes
from my_cloudfly.billing.utils.cart import update_order_items
from my_cloudfly.osbilling.price_simulator import PriceSimulatorUtils, PriceSimulator
from my_cloudfly.users.settings import BillingSettings
from my_cloudfly.core.utils import cdecimal

ReosurceTypes = PriceSimulator.ResourceTypes


class OrderItemProductProgram:
    def __init__(self, item, product, programs):
        self.item = item
        self.product = product
        self.programs = programs

class ItemProgram:
    def __init__(self, item: OrderItem, program: LoyaltyProgram):
        self.item = item
        self.program = program

class OrderLoyaltyProgram:

    def __init__(self, order: Order):
        self.order = order
        self.billing_settings: BillingSettings = self.order.client.billing_settings
        self.currency = self.order.currency
        self._traits = self.__compute_traits()
        self._coupon: List[OrderPointReward] = list()
        self.item_product_programs: List[OrderItemProductProgram] = list()
        self.item_programs: List[ItemProgram] = list()

    @property
    def client(self):
        return self.order.client

    @property
    def user(self):
        return self.client.users.first()

    @property
    def order_items(self):
        return self.order.items.all()

    @property
    def total_amount(self):
        return self.order_items.total_origin()

    @property
    def total_order_amount(self):
        total_order_item_program = 0
        for item_program in self.item_product_programs:
            total_order_item_program += item_program.item.amount_origin
        return total_order_item_program

    @staticmethod
    def is_one_time(order_item: OrderItem) -> bool:
        return bool(
            order_item.service and
            order_item.service.cycle.cycle == 'onetime'
        )

    def __compute_traits(self):
        pass

    def add_item_product_program(self, item, product, programs):
        self.item_product_programs.append(OrderItemProductProgram(item, product, programs))

    def get_active_programs(self, code=None, program_type: str = None):
        """the method returm all Loyalty Program applicable to order
        :rtype: dict
        """
        for item in self.order_items.filter(product__isnull=False).select_related('product'):
            if item.service and item.service.is_price_overridden and item.service.cycle == item.cycle:
                # Đang sử dụng trọn đời cùng chu kỳ không thể áp dụng chương trình
                continue
            product_and_programs = LoyaltyProgram.objects.exclude(
                program_type=ProgramTypes.wallet
            ).active(
                code=code, program_type=program_type
            ).available_product(item.product, item.cycle)

            if product_and_programs:
                self.add_item_product_program(
                    item=item, product=item.product, programs=product_and_programs[item.product]
                )

    # def _order_apply_program(self, code=None):
    #     return self._apply_program(self._order_program_check_compute_points(code=code))

    def _apply_program(self, programs_and_point):
        """Create order point reward for order and return list order point created

        :rtype: list OrderPointReward
        """
        if settings.ENVIRONMENT_TEST:
            quantity_program_apply = 5
        else:
            quantity_program_apply = self.billing_settings.maximum_quantity_program_order_possible_apply
        # programs ordered by priority
        loop_count = 0
        for program, data in programs_and_point.items():
            loop_count += 1
            order_points = self.order.orderpointreward_set.filter(program=program, client=self.client).first()
            if not order_points:
                order_points = OrderPointReward.objects.create(
                    order=self.order,
                    points=data['points'],
                    program=program,
                    client=self.client,
                )
            elif order_points.points != data['points']:
                order_points.points = data['points']
                order_points.save(update_fields=['points'])
            self._coupon.append(order_points)
            # if loop_count == quantity_program_apply:
            #     break
        return self._coupon

    def _get_claimable_rewards(self):
        """
        Fetch all rewards that are currently claimable from all concerned coupons,
         meaning coupons from applied programs and applied rewards or the coupons given as parameter.

        Returns a dict containing the all the claimable rewards grouped by coupon.
        Coupons that can not claim any reward are not contained in the result.
        """
        total_is_zero = cdecimal(self.total_order_amount, q="0.0001") < 1
        result: dict = dict()
        for order_point_reward in self._coupon:
            rewards: "QuerySet[RewardProgram]" = order_point_reward.program.program_rewards.all()
            result_rewards: List[RewardProgram] = []
            for reward in rewards:
                if reward.reward_type == RewardProgram.DISCOUNT and total_is_zero:
                    continue
                # if not reward.program.unlimited and reward._remain_quantity(self.user) < 1:
                #     continue
                if reward.required_point > order_point_reward.points:
                    continue
                result_rewards.append(reward)
            if result_rewards:
                result[order_point_reward] = rewards.filter(
                    id__in=[reward.id for reward in result_rewards]
                ).order_by("-discount")
        return result

    def _order_try_apply_coupon(self, code):
        self.get_active_programs(code)
        if not self.item_product_programs:
            return False, "No program available"
        programs_and_point = self._order_program_check_compute_points(code=code)
       
        if not programs_and_point:
            return False, "No program available"
        if isinstance(programs_and_point, dict):
            point_and_error = programs_and_point.get(next(iter(programs_and_point))) #  {program: {'points': number, 'error': str}} => {'points': number, 'error': str}
            if 'error' in point_and_error:
                return False, point_and_error.get("error")

        if not self._apply_program(programs_and_point):
            return False, _("Can not apply program")
        # Get all claimable rewards to order
        coupon_and_reward = self._get_claimable_rewards()
        if len(coupon_and_reward) == 1:
            coupon = next(iter(coupon_and_reward))
            if coupon_and_reward[coupon] is not None:
                reward_large = coupon_and_reward[coupon][0]
                return self._order_apply_reward(reward_large, coupon), _("Applied coupon")
        return False, _("Can not apply coupon")

    def _get_rewards_type_promotion(self):
        self.get_active_programs(code=None, program_type=ProgramTypes.promotion)
        if not self.item_product_programs:
            return False
        if not self._apply_program(self._order_program_check_compute_points()):
            return False
        return self._get_claimable_rewards()

    def _order_try_apply_reward(self, reward: RewardProgram) -> Tuple[bool, str]:
        program = reward.program
        if (
            program.loyalty_cards.exists() 
            and not program.loyalty_cards.filter(
                Q(expiration_time__isnull=True)
                | Q(expiration_time__gt=timezone.now())
            ).filter(Q(partner=self.user) | Q(partner__isnull=True)).exists()
        ):
            return False, _("Program not available")
        self.get_active_programs(code=None, program_type=ProgramTypes.promotion)
        if not self.item_product_programs:
            return False, _("Program not available")
        programs_and_point = self._order_program_check_compute_points()
        if isinstance(programs_and_point, dict):
            point_and_error = programs_and_point.get(reward.program)  # {program: {'points': number, 'error': str}} => {'points': number, 'error': str}
            if not point_and_error:
                return False, _("Program not available")
            if 'error' in point_and_error:
                return False, point_and_error.get("error", _("Program not available"))

        if not self._apply_program(programs_and_point):
            return False, _("Can not apply program")
        order_point_rewards = self._get_claimable_rewards()
        if not order_point_rewards:
            return False, _("Can not claim program")
        if reward not in list(chain.from_iterable(order_point_rewards.values())):  # [[reward1], ..., [reward2, ...]] => [reward1, reward2]
            return False, _("Program not available")

        for order_point_reward, rewards in order_point_rewards.items():
            if reward in rewards:
                self._order_apply_reward(reward, order_point_reward)
                break
        return True, _("Applied program")

    def _order_apply_reward(self, reward, coupon):
        """
        Applies the reward to the order provided the given coupon has enough points.
        This method does not check for program rules.
        """
        if reward.reward_type != RewardProgram.DISCOUNT:
            return None
        if reward.discount_applicability != RewardProgram.APPLY_ORDER:
            return False
        if not self.item_product_programs:
            return False
        order_items = list(map(
                lambda item_program: item_program.item,
                list(filter(
                    lambda item_program: item_program.program == reward.program,
                    self.item_programs
                ))
            )
        ) # [{item, program}] = > [item]
        total_discount = decimal.Decimal(reward.discount / len(order_items))
        for item in order_items:
            if item.item_type == OrderItemTypes.service:
                fixed_price = item.fixed_price
            elif item.item_type in OrderItemTypes.types_require_service:
                if not item.service:
                    raise APIException('Item missing service.')
                if item.item_type in [OrderItemTypes.serviceUpgrade, OrderItemTypes.serviceResize] or item.service.cycle.cycle == 'onetime':
                    fixed_price = item.fixed_price
                else:
                    try:
                        if item.service.cycle.cycle == item.cycle.cycle:
                            fixed_price = item.service.get_fixed_price(override_price=False) / (item.service.cycle.cycle_multiplier) * decimal.Decimal(item.cycle.cycle_multiplier)
                        else:
                            fixed_price = item.service.get_fixed_price(override_price=False)
                            if item.service.cycle.cycle == 'month':
                                price_service_per_month = fixed_price / item.service.cycle.cycle_multiplier
                            else:
                                price_service_per_month = fixed_price / 12 / item.service.cycle.cycle_multiplier
                            if item.cycle.cycle == 'month':
                                fixed_price = price_service_per_month * item.cycle.cycle_multiplier
                            else:
                                fixed_price = price_service_per_month * item.cycle.cycle_multiplier * 12
                    except Exception as e:
                        raise APIException(str(e))
            else:
                raise APIException(_('Unable get price'))

            if reward.discount_mode == 'percent':
                price = fixed_price * (1 - Decimal(reward.discount) / 100)
                discount = reward.discount
            else:
                price = fixed_price - total_discount
                discount = (total_discount / fixed_price) * 100
            if item.item_type == OrderItemTypes.service:
                for configurable_options in item.configurable_options.all():
                    option_cycle = configurable_options.option.cycles.first()
                    if option_cycle.price_type == ConfigurableOptionCycle.PRICE_TYPES.percentage:
                        option_price = option_cycle.convert_price_type_percentage_to_base_price(price=price)
                    else:
                        option_price = option_cycle.price
                    base_price = option_price * configurable_options.quantity
                    configurable_options.price = base_price
                    # configurable_options.unit_price = base_price
                    configurable_options.save(update_fields=['price', 'unit_price'])
            else:
                price = price - item.configurable_options_price
                fixed_price = fixed_price - item.configurable_options_price
            item.fixed_price = cdecimal(fixed_price, q=1)
            item.total = cdecimal(price, q=1)
            item.discount = discount
            item.program_reward = reward
            item.order_point_reward = coupon
            item.save(update_fields=['total', 'fixed_price', 'discount', 'program_reward', 'order_point_reward'])
        # reset order item remains
        order_item_remains = self.order_items.exclude(id__in=list(map(lambda item: item.id, order_items)))
        if order_item_remains:
            update_order_items(order_item_remains)

        return True

    # def _get_reward_values_discount(self, reward: RewardProgram, coupon: OrderPointReward):
    #     """ The method returm data to create order item discount to the order
    #     """
    #     if reward.reward_type != RewardProgram.DISCOUNT:
    #         return None
    #     discountable = 0
    #     reward_applies_on = reward.discount_applicability
    #     if reward_applies_on == RewardProgram.APPLY_ORDER:
    #         discountable, discountable_per_tax = self._get_disount_apply_order(reward=reward)
    #         # TODO: handler for another case
    #
    #     discountable = min(self._get_total_order_item_amount(reward), discountable)
    #     if reward.discount_max_value > 0:
    #         max_discount = b_utils.convert_currency(reward.discount_max_value, reward.currency, self.currency)
    #     else:
    #         max_discount = discountable
    #
    #     if reward.discount_mode == 'percent':
    #         max_discount = min(max_discount, discountable * Decimal(reward.discount / 100))
    #     else:
    #         max_discount = min(max_discount, reward.discount)
    #
    #     return {
    #         'name': reward.description,
    #         'total': -min(max_discount, discountable),
    #         'order_point_reward_id': coupon.id,
    #         'program_reward': reward,
    #         'description': reward.description
    #     }

    # def _get_disount_apply_order(self, reward: RewardProgram):
    #     """The the case type discount applicability of reward is `order`
    #     raise AssertErorr Exception if type invalid
    #
    #     :rtype: tupe discountable and discountable tax
    #     """
    #     assert reward.discount_applicability == RewardProgram.APPLY_ORDER
    #
    #     discountable = 0
    #     discountable = self._get_total_order_item_amount(reward)
    #     return discountable, discountable

    # def _get_total_order_item_amount(self, reward: RewardProgram) -> decimal.Decimal:
    #     total_order_amount = decimal.Decimal("0.00")
    #     for item_program in self.item_product_programs:
    #         for program in item_program.programs:
    #             if program.program_rewards.filter(id=reward.id).exists():
    #                 total_order_amount += item_program.item.amount_origin
    #                 break
    #     return total_order_amount

    def _order_program_check_compute_points(self, code=None):
        """
        Checks the program validity from the order items aswell as computing the number of points to add.

        Returns a dict containing the error message or the points that will be given with the keys 'points'.
        """
        result = dict()
        for ipp in self.item_product_programs:
            for program in ipp.programs:
                if code and not program.loyalty_cards.filter(code=code).filter(
                    Q(expiration_time__isnull=True) |
                    Q(expiration_time__gt=timezone.now())
                ).filter(Q(partner=self.user) | Q(partner__isnull=True)).exists():
                    continue
                if not program.unlimited and program.remaining_quantity(self.user) < 1:
                    continue
                conditions: "QuerySet[ProgramCondition]" = program.program_conditions.active()
                
                matched = conditions.exists() and program.applies_on == 'current'
                minimum_amount_matched = matched
                count_minimum_amount_matched = 0
                minimum_quantity_matched = matched
                # order_type_matched = matched
                count_order_type_matched = 0
                if program.is_has_math_condition:
                    product = ipp.product
                    if product.product_type == ReosurceTypes.instance:
                        plugin_data = ipp.item.plugin_data
                        if isinstance(plugin_data, dict):
                            traits = PriceSimulatorUtils.get_customize_instance_simulated_traits(
                                    vcpus=plugin_data.get('vcpus'),
                                    region=plugin_data.get('region_name'),
                                    root_gb=plugin_data.get('disk'),
                                    memory_mb=plugin_data.get('ram'),
                                    instance_type=plugin_data.get('flavor_name'),
                                    aggregate_instance=plugin_data.get('aggregate_instance'),
                            )
                            conditions = program.get_all_matching_reward_conditions(traits)
                        else:
                            continue
                    else:
                        # TODO: handler for anthor product (proxy, domain, ssl, ...)
                        pass
                points = 0

                for condition in conditions:
                    # Check minimum amount order
                    condition_minimum_amount = condition.get_minimun_amount(currency=self.currency)
                    if condition_minimum_amount > self.total_amount:
                        # TODO: handler the case condition include tax or exclude tax
                        minimum_amount_matched = False
                        continue
                    else:
                        minimum_amount_matched = True
                        count_minimum_amount_matched += 1
                    # TODO: Handler minimum quantity
                    minimum_quantity_matched = True

                    if program.applies_on == 'future':
                        # TODO: handler program appies on future
                        continue
                    else:
                        if condition.reward_point_mode == 'order':
                            # TODO: handler for another case
                            if condition.product_cycles.exists() and not condition.product_cycles.filter(id=ipp.item.cycle.id).exists():
                                continue
                            item_type = OrderItemTypes.service if self.is_one_time(ipp.item) else ipp.item.item_type
                            if condition.order_item_type and item_type != condition.order_item_type:
                                continue
                            else:
                                if not list(filter(
                                    lambda
                                        item_program: item_program.item == ipp.item and item_program.program == program,
                                        self.item_programs
                                )):
                                    total_order_item = self.order_items.filter(item_type=item_type).total_origin()
                                    if self.is_one_time(ipp.item):
                                        total_order_item += self.order_items.filter(item_type=ipp.item.item_type).total_origin()
                                    if total_order_item < condition_minimum_amount:
                                        minimum_amount_matched = False
                                        continue
                                    minimum_amount_matched = True
                                    self.item_programs.append(ItemProgram(item=ipp.item, program=program))

                                count_order_type_matched += 1
                                points += condition.reward_point_amount
                program_result: dict = dict(points=points)
                if not matched:
                    program_result['error'] = _("Can not apply program.")
                elif not minimum_amount_matched:
                    program_result['error'] = _(
                        'A minimum of %(amount)s %(code)s should be purchased to get the reward'
                    ) % {
                        'amount': min(conditions.values_list('minimum_amount', flat=True)),
                         'code': self.currency.code
                    }
                elif not minimum_quantity_matched:
                    program_result['error'] = _("Can not apply program.")
                elif count_order_type_matched == 0:
                    program_result['error'] = _("Can not apply program.")

                if program in result:
                    if 'error' in result[program]:
                        result[program] = program_result
                else:
                    result[program] = program_result
        return result


    def get_programs(self) -> list:
        self.get_active_programs(None, ProgramTypes.promotion)
        rewards = []
        for item in self.item_product_programs:
            for program in item.programs:
                if (
                    program.loyalty_cards.filter(code__isnull=False).exists() 
                    and not program.loyalty_cards.filter(
                        Q(expiration_time__isnull=True)
                        | Q(expiration_time__gt=timezone.now())
                    ).filter(Q(partner=self.user) | Q(partner__isnull=True)).exists()
                ):
                    continue
                rewards = list(set(rewards + list(program.program_rewards.all())))
        rewards_data = []
        for reward in rewards:
            serializer_data_reward: dict = dict(
                **RewardProgramClientSerializer(reward).data,
                can_apply=True,
                remain_usage=-1
            )
            total_is_zero = cdecimal(self.total_order_amount, q="0.0001") < 1
            if reward.reward_type == RewardProgram.DISCOUNT and total_is_zero:
                continue
            if not reward.program.unlimited:
                remain_usage = reward._remain_quantity(self.user)
                serializer_data_reward.update(remain_usage=remain_usage)
                if remain_usage < 1:
                    continue
            programs_and_point = self._order_program_check_compute_points()
            if not isinstance(programs_and_point, dict):
                continue
            point_and_error = programs_and_point.get(reward.program)  # {program: {'points': number, 'error': str}} => {'points': number, 'error': str}
            if not point_and_error:
                serializer_data_reward.update(
                    can_apply=False,
                    messages_error=_("Can not apply program."),
                )
                rewards_data.append(serializer_data_reward)
                continue
            if 'error' in point_and_error:
                serializer_data_reward.update(
                    can_apply=False,
                    messages_error=point_and_error.get("error"),
                )
                rewards_data.append(serializer_data_reward)
                continue

            if reward.required_point > int(point_and_error.get("points")):
                serializer_data_reward.update(
                    can_apply=False,
                    messages_error=_("You are not eligible"),
                )
                rewards_data.append(serializer_data_reward)
                continue
            rewards_data.append(serializer_data_reward)

        return rewards_data
