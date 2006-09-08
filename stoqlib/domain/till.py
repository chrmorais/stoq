# -*- coding: utf-8 -*-
# vi:si:et:sw=4:sts=4:ts=4

##
## Copyright (C) 2005, 2006 Async Open Source <http://www.async.com.br>
## All rights reserved
##
## This program is free software; you can redistribute it and/or modify
## it under the terms of the GNU Lesser General Public License as published by
## the Free Software Foundation; either version 2 of the License, or
## (at your option) any later version.
##
## This program is distributed in the hope that it will be useful,
## but WITHOUT ANY WARRANTY; without even the implied warranty of
## MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
## GNU Lesser General Public License for more details.
##
## You should have received a copy of the GNU Lesser General Public License
## along with this program; if not, write to the Free Software
## Foundation, Inc., or visit: http://www.gnu.org/.
##
## Author(s):    Henrique Romano        <henrique@async.com.br>
##               Evandro Vale Miquelito <evandro@async.com.br>
##
""" Implementation of classes related to Payment management. """

from datetime import datetime

from sqlobject import (IntCol, DateTimeCol, ForeignKey, BoolCol, UnicodeCol,
                       SQLObject)
from sqlobject.sqlbuilder import AND
from zope.interface import implements
from kiwi.datatypes import currency
from kiwi.log import Logger

from stoqlib.lib.translation import stoqlib_gettext
from stoqlib.lib.runtime import get_current_branch
from stoqlib.exceptions import (TillError, DatabaseInconsistency,
                                StoqlibError)
from stoqlib.lib.parameters import sysparam
from stoqlib.domain.columns import PriceCol, AutoIncCol
from stoqlib.domain.base import Domain, BaseSQLView
from stoqlib.domain.sale import Sale
from stoqlib.domain.payment.base import AbstractPaymentGroup, Payment
from stoqlib.domain.interfaces import (IPaymentGroup, ITillOperation,
                                       IOutPayment, IInPayment)
from stoqlib.domain.station import BranchStation

_ = stoqlib_gettext

#
# Domain Classes
#

log = Logger('stoqlib.till')

class Till(Domain):
    """A definition of till operation.

    B{Attributes}:
        - I{STATUS_PENDING}: this till have some sales unconfirmed when
                             closing the till of the last day but it's
                             not opened yet.
        - I{STATUS_OPEN}: this till is opened and we can make sales for it.
        - I{STATUS_CLOSED}: end of the day, the till is closed and no more
                            financial operations can be done in this store.
        - I{balance_sent}: the amount total sent to the warehouse or main
                           store after closing the till.
        - I{initial_cash_amount}: The total amount we have in the moment we
                                  are opening the till. This value is useful
                                  when providing change during sales.
        - I{station}: a till operation is always associated with a branch
                      station which means the computer in a branch company
                      responsible to open the till
    """

    (STATUS_PENDING,
     STATUS_OPEN,
     STATUS_CLOSED) = range(3)

    statuses = {STATUS_PENDING: _(u"Pending"),
                STATUS_OPEN:    _("Opened"),
                STATUS_CLOSED:  _("Closed")}

    status = IntCol(default=STATUS_PENDING)
    balance_sent = PriceCol(default=0)
    final_cash_amount = PriceCol(default=0)
    opening_date = DateTimeCol(default=datetime.now)
    closing_date = DateTimeCol(default=None)
    station = ForeignKey('BranchStation')

    def _get_payment_group(self):
        conn = self.get_connection()
        group = IPaymentGroup(self)
        if not group:
            group = self.addFacet(IPaymentGroup, connection=conn)
        return group

    #
    # Till methods
    #

    def get_entries(self):
        conn = self.get_connection()
        return TillFiscalOperationsView.selectBy(till_id=self.id,
                                                 connection=conn)
    def get_cash_total(self):
        conn = self.get_connection()
        entries = TillEntry.selectBy(tillID=self.id, connection=conn)
        total = sum([entry.value for entry in entries], currency(0))
        return currency(total)

    def get_initial_cash_amount(self):
        q1 = TillFiscalOperationsView.q.till_id == self.id
        q2 = TillFiscalOperationsView.q.is_initial_cash_amount == True
        query = AND(q1, q2)
        conn = self.get_connection()
        entries = TillFiscalOperationsView.select(query, connection=conn)
        count = entries.count()
        if count > 1 or not count:
            raise DatabaseInconsistency("You should have only one initial "
                                        "cash amount entry at this point")
        return entries[0].value

    def get_float_remaining(self):
        return currency(self.get_balance() - self.balance_sent)

    def get_balance(self):
        """ Return the total of all "extra" payments (like cash
        advance, till complement, ...) associated to this till
        operation *plus* all the payments, which payment method is
        money, of all the sales associated with this operation
        *plus* the initial cash amount.
        """
        entries = self.get_entries()
        total = sum([entry.value for entry in entries], currency(0))
        return currency(total)

    def open_till(self):
        conn = self.get_connection()
        last_till = get_last_till_operation_for_current_branch(conn)
        if last_till:
            final_cash = last_till.final_cash_amount
            if final_cash > 0:
                reason = _(u'Cash amount remaining of %s'
                           % last_till.closing_date.strftime('%x'))
                self.create_credit(final_cash, reason)

            sales = last_till.get_unconfirmed_sales()
            for sale in sales:
                sale.till = self

        if not IPaymentGroup(self):
            # Add a IPaymentGroup facet for the new till and make it easily
            # available to receive new payments
            self.addFacet(IPaymentGroup, connection=conn)

    def get_unconfirmed_sales(self):
        conn = self.get_connection()
        sales = Sale.get_available_sales(conn, self)
        return [sale for sale in sales
                    if sale.status != Sale.STATUS_CONFIRMED]

    def get_credits_total(self):
        entries = self.get_entries()
        total = sum([entry.value for entry in entries
                     if entry.value > 0], currency(0))
        return currency(total)

    def get_debits_total(self):
        entries = self.get_entries()
        total = sum([entry.value for entry in entries
                     if entry.value < 0], currency(0))
        return currency(total)

    def close_till(self):
        """ This method close the current till operation with the confirmed
        sales associated. If there is a sale with a differente status than
        SALE_CONFIRMED, a new 'pending' till operation is created and
        these sales are associated with the current one.
        """

        if self.status == Till.STATUS_CLOSED:
            raise StoqlibError("This till is already closed. Open a new till "
                               "before close it.")
        conn = self.get_connection()
        sales = Sale.get_available_sales(conn, self)
        money_payment_method = sysparam(conn).METHOD_MONEY

        for sale in sales:
            if sale.status != Sale.STATUS_CONFIRMED:
                continue
            group = IPaymentGroup(sale)
            if not group:
                raise DatabaseInconsistency("Sale must have a"
                                            "IPaymentGroup facet")
            for payment in group.get_items():
                payment.status = Payment.STATUS_TO_PAY

        current_balance = self.get_balance()
        if self.balance_sent and self.balance_sent > current_balance:
            raise ValueError("The cash amount that you want to send is "
                             "greater than the current balance.")
        self.status = self.STATUS_CLOSED
        self.closing_date = datetime.now()
        self.final_cash_amount = current_balance - self.balance_sent

    def create_debit(self, value, reason):
        conn = self.get_connection()
        group = self._get_payment_group()
        return group.create_debit(value, reason, self)

    def create_credit(self, value, reason):
        conn = self.get_connection()
        group = self._get_payment_group()
        return group.create_credit(value, reason, self)

    @classmethod
    def get_current(cls, conn):
        """
        Fetches the Till for the current branch.
        @param conn: a database connection
        @returns: a Till instance or None
        """
        branch = get_current_branch(conn)
        result = cls.select(AND(cls.q.status == Till.STATUS_OPEN,
                                cls.q.stationID == BranchStation.q.id,
                                 BranchStation.q.branchID == branch.id),
                            connection=conn)
        if result.count() > 1:
            raise TillError(
                "You should have only one Till opened. Got %d instead." %
                result.count())
        elif result.count() == 0:
            return None
        return result[0]


class TillEntry(Domain):
    # It's usefull to use the same sequence of Payment table since we want
    # sometimes do mix payments and till entries in the same database view,
    # so we can search properly by identifier field
    identifier = AutoIncCol("stoqlib_payment_identifier_seq")
    date = DateTimeCol(default=datetime.now)
    description = UnicodeCol()
    value = PriceCol()
    is_initial_cash_amount = BoolCol(default=False)
    till = ForeignKey("Till")
    payment_group = ForeignKey("AbstractPaymentGroup", default=None)


#
# Adapters
#


class TillAdaptToPaymentGroup(AbstractPaymentGroup):
    implements(IPaymentGroup, ITillOperation)

    #
    # ITillOperation implementation
    #

    def add_debit(self, value, reason, category, date=None):
        payment = self.add_payment(value, reason, category, date)

        return payment.addFacet(IOutPayment)

    def add_credit(self, value, reason, category, date=None):
        payment = self.add_payment(value, reason, category, date)

        return payment.addFacet(IInPayment)

    def add_complement(self, value, reason, category, date=None):
        raise NotImplementedError

    def add_expenditure(self, value, reason, category, date=None):
        raise NotImplementedError

    def get_cash_advance(self, value, reason, category, employee, date=None):
        raise NotImplementedError

    def cancel_payment(self, payment, reason, date=None):
        raise NotImplementedError

    #
    # IPaymentGroup implementation
    #

    def get_thirdparty(self):
        branch = self.get_adapted().branch
        return branch.get_adapted()

    def set_thirdparty(self):
        raise NotImplementedError

    def get_group_description(self):
        till = self.get_adapted()
        date_format = _(u'%d of %B')
        today_str = till.opening_date.strftime(date_format)
        return _(u'till of %s') % today_str


Till.registerFacet(TillAdaptToPaymentGroup, IPaymentGroup)


#
# Views
#


class TillFiscalOperationsView(SQLObject, BaseSQLView):
    """Stores informations about till fiscal operations, which is a union between
    till_entry and payment tables
    """

    identifier = IntCol()
    date = DateTimeCol()
    closing_date = DateTimeCol()
    description = UnicodeCol()
    value = PriceCol()
    is_initial_cash_amount = IntCol()
    till_id = IntCol()
    station_name = UnicodeCol()
    branch_id = IntCol()
    status = IntCol()


#
# Functions
#


def get_last_till_operation_for_current_branch(conn):
    """  The last till operation is used to get a initial cash amount
    to a new till operation that will be created, this value is based
    on the final_cash_amount attribute of the last till operation
    """

    table = TillFiscalOperationsView
    query = AND(table.q.status == Till.STATUS_CLOSED,
                table.q.branch_id == get_current_branch(conn).id)
    result = table.select(query, connection=conn).orderBy('closing_date')
    if not result.count():
        return
    till_entry = result[-1]
    return Till.get(till_entry.till_id, connection=conn)
