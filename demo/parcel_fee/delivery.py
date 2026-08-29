"""校园快递配送费计算。"""


def calculate_delivery_fee(
    subtotal_yuan: float, weight_kg: float, is_member: bool = False
) -> int:
    """按订单金额、包裹重量和会员身份返回配送费（元）。"""

    if subtotal_yuan < 0:
        raise ValueError("订单金额不能为负数")
    if weight_kg <= 0:
        raise ValueError("包裹重量必须大于 0")

    free_threshold = 69 if is_member else 99
    if subtotal_yuan > free_threshold:
        return 0

    extra_kilograms = max(0, int(weight_kg) - 1)
    return 8 + extra_kilograms * 2
